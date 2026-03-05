"""
Meta Ads Slack Slash Command App
==================================
Slash commands:
    /daily  — Yesterday's Meta Ads report sent as DM
    /weekly — Last full Mon-Sun week report sent as DM

Fixes:
    - Deduplicates omni_purchase vs purchase (picks omni if available, else purchase)
    - Filters out campaigns with less than $1 spend
    - Correctly aggregates totals

Requirements:
    pip install flask requests python-dotenv gunicorn

.env variables:
    META_ACCESS_TOKEN     — Long-lived Meta access token
    META_AD_ACCOUNT_ID    — e.g. act_785368271166897
    SLACK_BOT_TOKEN       — xoxb-... from Slack app OAuth & Permissions
    SLACK_SIGNING_SECRET  — From Slack app Basic Information
"""

import os
import json
import hmac
import hashlib
import time
import threading
import requests
from datetime import date, timedelta
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

META_ACCESS_TOKEN    = os.getenv("META_ACCESS_TOKEN")
META_AD_ACCOUNT_ID   = os.getenv("META_AD_ACCOUNT_ID")
SLACK_BOT_TOKEN      = os.getenv("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")

META_API_VERSION = "v19.0"
META_API_BASE    = f"https://graph.facebook.com/{META_API_VERSION}"

MIN_SPEND = 1.0  # Filter out campaigns with less than $1 spend

# ── Date helpers ───────────────────────────────────────────────────────────────

def get_daily_range():
    yesterday = date.today() - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d"), "daily"

def get_weekly_range():
    today = date.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday.strftime("%Y-%m-%d"), last_sunday.strftime("%Y-%m-%d"), "weekly"

# ── Slack signature verification ───────────────────────────────────────────────

def verify_slack_signature(req):
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")
    if abs(time.time() - int(timestamp)) > 300:
        return False
    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)

# ── Meta Ads helpers ───────────────────────────────────────────────────────────

def parse_roas(row):
    """Pick omni_purchase ROAS first, fall back to purchase."""
    roas_raw = row.get("purchase_roas", [])
    omni = next((float(i["value"]) for i in roas_raw if i.get("action_type") == "omni_purchase"), None)
    if omni is not None:
        return omni
    return next((float(i["value"]) for i in roas_raw if i.get("action_type") == "purchase"), None)

def parse_action(items, *types):
    """
    Deduplicate omni_ vs non-omni action types.
    If omni_ version exists, use it. Otherwise use the standard one.
    Never sum both.
    """
    if not items:
        return 0
    lookup = {i.get("action_type"): float(i.get("value", 0)) for i in items}

    results = []
    for t in types:
        omni_key = f"omni_{t}" if not t.startswith("omni_") else t
        std_key  = t.replace("omni_", "") if t.startswith("omni_") else t
        if omni_key in lookup:
            results.append(lookup[omni_key])
        elif std_key in lookup:
            results.append(lookup[std_key])

    return int(round(sum(results))) if results else 0

def parse_action_value(items, *types):
    """Same deduplication logic for action_values."""
    if not items:
        return None
    lookup = {i.get("action_type"): float(i.get("value", 0)) for i in items}

    total = 0
    found = False
    for t in types:
        omni_key = f"omni_{t}" if not t.startswith("omni_") else t
        std_key  = t.replace("omni_", "") if t.startswith("omni_") else t
        if omni_key in lookup:
            total += lookup[omni_key]
            found = True
        elif std_key in lookup:
            total += lookup[std_key]
            found = True

    return round(total, 2) if found else None

def fetch_meta_insights(since: str, until: str):
    url = f"{META_API_BASE}/{META_AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "campaign_name,spend,impressions,inline_link_clicks,cpm,cpc,ctr,purchase_roas,actions,action_values",
        "time_range": json.dumps({"since": since, "until": until}),
        "level": "campaign",
        "limit": 50,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("data"):
        return None, None

    campaigns = []
    for row in data["data"]:
        spend = float(row.get("spend", 0))

        # Skip campaigns with less than $1 spend
        if spend < MIN_SPEND:
            continue

        actions     = row.get("actions", [])
        action_vals = row.get("action_values", [])

        campaigns.append({
            "name":        row.get("campaign_name", "Unknown"),
            "spend":       spend,
            "cpm":         float(row.get("cpm", 0)),
            "cpc":         float(row.get("cpc", 0)),
            "ctr":         float(row.get("ctr", 0)),
            "impressions": int(row.get("impressions", 0)),
            "clicks":      int(row.get("inline_link_clicks", 0)),
            "roas":        parse_roas(row),
            "carts":       parse_action(actions,     "add_to_cart"),
            "checkouts":   parse_action(actions,     "initiate_checkout"),
            "orders":      parse_action(actions,     "purchase"),
            "order_value": parse_action_value(action_vals, "purchase"),
        })

    if not campaigns:
        return None, None

    campaigns.sort(key=lambda x: x["spend"], reverse=True)

    total_spend       = sum(c["spend"]       for c in campaigns)
    total_impressions = sum(c["impressions"] for c in campaigns)
    total_clicks      = sum(c["clicks"]      for c in campaigns)

    def wavg(key):
        return sum(c[key] * c["spend"] for c in campaigns) / total_spend if total_spend else 0

    # CTR = clicks / impressions * 100 (correct, not spend-weighted)
    total_ctr = (total_clicks / total_impressions * 100) if total_impressions else 0

    rc = [c for c in campaigns if c["roas"] is not None]
    roas_total = None
    if rc:
        rs = sum(c["spend"] for c in rc)
        roas_total = sum(c["roas"] * c["spend"] for c in rc) / rs if rs else None

    ov_vals = [c["order_value"] for c in campaigns if c["order_value"] is not None]

    totals = {
        "spend":       total_spend,
        "cpm":         wavg("cpm"),
        "cpc":         wavg("cpc"),
        "ctr":         total_ctr,
        "roas":        roas_total,
        "carts":       sum(c["carts"]     for c in campaigns),
        "checkouts":   sum(c["checkouts"] for c in campaigns),
        "orders":      sum(c["orders"]    for c in campaigns),
        "order_value": round(sum(ov_vals), 2) if ov_vals else None,
    }

    return campaigns, totals

# ── Message builder ────────────────────────────────────────────────────────────

def roas_emoji(roas):
    if roas is None: return "📊"
    if roas >= 3:    return "🟢"
    if roas >= 1.5:  return "🟡"
    return "🔴"

def fmt_roas(v): return f"{v:.2f}x" if v is not None else "N/A"
def fmt_ov(v):   return f"${v:,.2f}" if v is not None else "N/A"

def build_message(campaigns, totals, since, until, period, triggered_by=None):
    footer = f"Requested by <@{triggered_by}>" if triggered_by else "Automated"

    if period == "daily":
        title        = f"{roas_emoji(totals['roas'] if totals else None)}  Meta Ads Daily Report"
        period_label = f"📅  *{since}*"
    else:
        title        = f"{roas_emoji(totals['roas'] if totals else None)}  Meta Ads Weekly Report"
        period_label = f"📅  *{since}  →  {until}*"

    if campaigns is None:
        return {
            "text": title,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"*{title}*\n\n_No spend data found for this period._"}}]
        }

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"{period_label}  ·  {footer}  ·  _Campaigns with $1+ spend only_"}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📣 Campaign Breakdown*"}},
    ]

    for i, c in enumerate(campaigns, 1):
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{i}. {c['name']}*\n"
                    f"> 💸 Spend: `${c['spend']:,.2f}`  ·  "
                    f"📦 CPM: `${c['cpm']:,.2f}`  ·  "
                    f"🖱 CPC: `${c['cpc']:,.2f}`  ·  "
                    f"👆 CTR: `{c['ctr']:.2f}%`  ·  "
                    f"💰 ROAS: `{fmt_roas(c['roas'])}`\n"
                    f"> 🛒 Carts: `{c['carts']}`  ·  "
                    f"🏁 Checkouts: `{c['checkouts']}`  ·  "
                    f"📦 Orders: `{c['orders']}`  ·  "
                    f"💵 Order Value: `{fmt_ov(c['order_value'])}`"
                )
            }
        })

    roas_str = fmt_roas(totals["roas"])
    ov_str   = fmt_ov(totals["order_value"])

    blocks += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📦 TOTAL — All Campaigns*"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*💸 Spend*\n${totals['spend']:,.2f}"},
                {"type": "mrkdwn", "text": f"*📦 CPM*\n${totals['cpm']:,.2f}"},
                {"type": "mrkdwn", "text": f"*🖱 CPC*\n${totals['cpc']:,.2f}"},
                {"type": "mrkdwn", "text": f"*👆 CTR*\n{totals['ctr']:.2f}%"},
                {"type": "mrkdwn", "text": f"*💰 ROAS*\n{roas_str}"},
                {"type": "mrkdwn", "text": f"*🛒 Carts*\n{totals['carts']}"},
                {"type": "mrkdwn", "text": f"*🏁 Checkouts*\n{totals['checkouts']}"},
                {"type": "mrkdwn", "text": f"*📦 Orders*\n{totals['orders']}"},
                {"type": "mrkdwn", "text": f"*💵 Order Value*\n{ov_str}"},
            ]
        },
        {"type": "divider"},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
             "text": "Meta Ads API  ·  `/daily` for yesterday  ·  `/weekly` for last Mon–Sun"}]}
    ]

    return {"text": title, "blocks": blocks}

# ── Slack DM sender ────────────────────────────────────────────────────────────

def send_dm(user_id: str, message: dict):
    resp = requests.post(
        "https://slack.com/api/conversations.open",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"users": user_id},
        timeout=10
    )
    channel_id = resp.json()["channel"]["id"]
    payload = {"channel": channel_id, **message}
    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                 "Content-Type": "application/json"},
        json=payload,
        timeout=15
    )

# ── Background worker ──────────────────────────────────────────────────────────

def fetch_and_send(user_id, since, until, period):
    try:
        campaigns, totals = fetch_meta_insights(since, until)
        message = build_message(campaigns, totals, since, until, period, triggered_by=user_id)
        send_dm(user_id, message)
    except Exception as e:
        send_dm(user_id, {
            "text": f"❌ Error: {str(e)}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"❌ Something went wrong:\n```{str(e)}```"}}]
        })

# ── Slash command handler ──────────────────────────────────────────────────────

def handle_command(period: str):
    if not verify_slack_signature(request):
        return jsonify({"error": "Invalid signature"}), 403

    user_id = request.form.get("user_id")

    if period == "daily":
        since, until, label = get_daily_range()
        ack = "⏳ Fetching yesterday's report... I'll DM it to you in a moment!"
    else:
        since, until, label = get_weekly_range()
        ack = f"⏳ Fetching weekly report ({since} → {until})... I'll DM it to you in a moment!"

    threading.Thread(target=fetch_and_send, args=(user_id, since, until, label), daemon=True).start()
    return jsonify({"response_type": "ephemeral", "text": ack})

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/slack/daily", methods=["POST"])
def daily():
    return handle_command("daily")

@app.route("/slack/weekly", methods=["POST"])
def weekly():
    return handle_command("weekly")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)