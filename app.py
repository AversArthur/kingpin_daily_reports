"""
Meta Ads Slack Slash Command App
==================================
Slash commands:
    /meta-daily  — Yesterday's Meta Ads report
    /meta-weekly — Last full week (Mon–Sun) Meta Ads report

When triggered, the report is sent as a DM to the user who ran the command.

Requirements:
    pip install flask requests python-dotenv gunicorn

.env variables:
    META_ACCESS_TOKEN     — Long-lived Meta access token
    META_AD_ACCOUNT_ID    — e.g. act_785368271166897
    SLACK_BOT_TOKEN       — Bot token (xoxb-...)
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

META_API_VERSION     = "v19.0"
META_API_BASE        = f"https://graph.facebook.com/{META_API_VERSION}"

# ── Date helpers ───────────────────────────────────────────────────────────────

def get_daily_range():
    """Yesterday."""
    yesterday = date.today() - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d"), yesterday.strftime("%d %b %Y")

def get_weekly_range():
    """Last full Mon–Sun week."""
    today = date.today()
    last_sunday = today - timedelta(days=today.weekday() + 1)
    last_monday = last_sunday - timedelta(days=6)
    label = f"{last_monday.strftime('%d %b')} – {last_sunday.strftime('%d %b %Y')}"
    return last_monday.strftime("%Y-%m-%d"), last_sunday.strftime("%Y-%m-%d"), label

# ── Slack signature verification ───────────────────────────────────────────────

def verify_slack_signature(req):
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")
    if not timestamp or not signature:
        return False
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
    for item in row.get("purchase_roas", []):
        if item.get("action_type") in ("omni_purchase", "purchase"):
            return float(item["value"])
    return None

def parse_action(actions, *types):
    total = sum(float(i.get("value", 0)) for i in (actions or []) if i.get("action_type") in types)
    return int(round(total))

def parse_action_value(action_values, *types):
    vals = [float(i.get("value", 0)) for i in (action_values or []) if i.get("action_type") in types]
    return round(sum(vals), 2) if vals else None

def fetch_meta_insights(since: str, until: str):
    url = f"{META_API_BASE}/{META_AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "campaign_name,spend,cpm,cpc,ctr,purchase_roas,actions,action_values",
        "time_range": json.dumps({"since": since, "until": until}),
        "level": "campaign",
        "limit": 20,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("data"):
        return None, None

    campaigns = []
    for row in data["data"]:
        spend       = float(row.get("spend", 0))
        actions     = row.get("actions", [])
        action_vals = row.get("action_values", [])
        campaigns.append({
            "name":        row.get("campaign_name", "Unknown Campaign"),
            "spend":       spend,
            "cpm":         float(row.get("cpm", 0)),
            "cpc":         float(row.get("cpc", 0)),
            "ctr":         float(row.get("ctr", 0)),
            "roas":        parse_roas(row),
            "carts":       parse_action(actions,     "add_to_cart",       "omni_add_to_cart"),
            "checkouts":   parse_action(actions,     "initiate_checkout", "omni_initiated_checkout"),
            "orders":      parse_action(actions,     "purchase",          "omni_purchase"),
            "order_value": parse_action_value(action_vals, "purchase",    "omni_purchase"),
        })

    campaigns.sort(key=lambda x: x["spend"], reverse=True)

    total_spend = sum(c["spend"] for c in campaigns)
    def wavg(key):
        return sum(c[key] * c["spend"] for c in campaigns) / total_spend if total_spend else 0

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
        "ctr":         wavg("ctr"),
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
def fmt_ov(v):   return f"£{v:,.2f}" if v is not None else "N/A"

def build_message(campaigns, totals, period_label: str, report_type: str, user_id: str):
    type_icon  = "📅" if report_type == "daily" else "📆"
    type_label = "Daily Report" if report_type == "daily" else "Weekly Report"

    if campaigns is None:
        return {
            "text": f"📊 Meta Ads {type_label} — {period_label}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"*📊 Meta Ads {type_label} — {period_label}*\n\n_No spend data found for this period._"}}]
        }

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text",
                     "text": f"{roas_emoji(totals['roas'])}  Meta Ads {type_label}", "emoji": True}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": f"{type_icon}  *{period_label}*  ·  Requested by <@{user_id}>"}]
        },
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
                    f"> 💸 Spend: `£{c['spend']:,.2f}`  ·  "
                    f"📦 CPM: `£{c['cpm']:,.2f}`  ·  "
                    f"🖱 CPC: `£{c['cpc']:,.2f}`  ·  "
                    f"👆 CTR: `{c['ctr']:.2f}%`  ·  "
                    f"💰 ROAS: `{fmt_roas(c['roas'])}`\n"
                    f"> 🛒 Carts: `{c['carts']}`  ·  "
                    f"🏁 Checkouts: `{c['checkouts']}`  ·  "
                    f"📦 Orders: `{c['orders']}`  ·  "
                    f"💵 Order Value: `{fmt_ov(c['order_value'])}`"
                )
            }
        })

    blocks += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📦 TOTAL — All Campaigns*"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*💸 Spend*\n£{totals['spend']:,.2f}"},
                {"type": "mrkdwn", "text": f"*📦 CPM*\n£{totals['cpm']:,.2f}"},
                {"type": "mrkdwn", "text": f"*🖱 CPC*\n£{totals['cpc']:,.2f}"},
                {"type": "mrkdwn", "text": f"*👆 CTR*\n{totals['ctr']:.2f}%"},
                {"type": "mrkdwn", "text": f"*💰 ROAS*\n{fmt_roas(totals['roas'])}"},
                {"type": "mrkdwn", "text": f"*🛒 Carts*\n{totals['carts']}"},
                {"type": "mrkdwn", "text": f"*🏁 Checkouts*\n{totals['checkouts']}"},
                {"type": "mrkdwn", "text": f"*📦 Orders*\n{totals['orders']}"},
                {"type": "mrkdwn", "text": f"*💵 Order Value*\n{fmt_ov(totals['order_value'])}"},
            ]
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": "Meta Ads API  ·  `/meta-daily` for yesterday  ·  `/meta-weekly` for last week"}]
        }
    ]

    return {"text": f"Meta Ads {type_label} — {period_label}", "blocks": blocks}

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

def fetch_and_send(user_id: str, report_type: str):
    try:
        if report_type == "daily":
            since, until, label = get_daily_range()
        else:
            since, until, label = get_weekly_range()

        campaigns, totals = fetch_meta_insights(since, until)
        message = build_message(campaigns, totals, label, report_type, user_id)
        send_dm(user_id, message)

    except Exception as e:
        send_dm(user_id, {
            "text": f"❌ Error fetching Meta Ads data: {str(e)}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"❌ Something went wrong:\n```{str(e)}```"}}]
        })

# ── Slash command endpoints ────────────────────────────────────────────────────

def handle_command(report_type: str):
    if not verify_slack_signature(request):
        return jsonify({"error": "Invalid signature"}), 403

    user_id = request.form.get("user_id")
    threading.Thread(target=fetch_and_send, args=(user_id, report_type), daemon=True).start()

    label = "yesterday's" if report_type == "daily" else "last week's"
    return jsonify({
        "response_type": "ephemeral",
        "text": f"⏳ Fetching {label} Meta Ads report... I'll DM it to you in a moment!"
    })

@app.route("/slack/meta-daily", methods=["POST"])
def meta_daily():
    return handle_command("daily")

@app.route("/slack/meta-weekly", methods=["POST"])
def meta_weekly():
    return handle_command("weekly")

# ── Health check ───────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    app.run(host="0.0.0.0", port=port)