"""
Meta Ads → Slack Daily Reporter
================================
Pulls yesterday's Meta Ads data per campaign and posts to Slack via webhook.
Runs automatically via GitHub Actions at 08:00 UK time.

Metrics: Spend, CPM, CPC, CTR, ROAS, Carts, Checkouts, Orders, Order Value

Fixes:
    - Deduplicates omni_purchase vs purchase (no double counting)
    - Filters out campaigns with less than $1 spend

Requirements:
    pip install requests python-dotenv

.env variables:
    META_ACCESS_TOKEN   — Long-lived Meta access token (ads_read scope)
    META_AD_ACCOUNT_ID  — e.g. act_785368271166897
    SLACK_WEBHOOK_URL   — Incoming webhook URL for your Slack channel
"""

import os
import json
import requests
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────

META_ACCESS_TOKEN  = os.getenv("META_ACCESS_TOKEN")
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID")
SLACK_WEBHOOK_URL  = os.getenv("SLACK_WEBHOOK_URL")

META_API_VERSION   = "v19.0"
META_API_BASE      = f"https://graph.facebook.com/{META_API_VERSION}"

MIN_SPEND = 1.0  # Filter out campaigns with less than $1 spend

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_yesterday():
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

def parse_roas(row):
    """Pick omni_purchase ROAS first, fall back to purchase."""
    roas_raw = row.get("purchase_roas", [])
    omni = next((float(i["value"]) for i in roas_raw if i.get("action_type") == "omni_purchase"), None)
    if omni is not None:
        return omni
    return next((float(i["value"]) for i in roas_raw if i.get("action_type") == "purchase"), None)

def parse_action(items, action_type):
    """
    Deduplicate omni_ vs standard action types.
    Prefer omni_ version if available, never sum both.
    """
    if not items:
        return 0
    lookup = {i.get("action_type"): float(i.get("value", 0)) for i in items}
    omni_key = f"omni_{action_type}"
    if omni_key in lookup:
        return int(round(lookup[omni_key]))
    if action_type in lookup:
        return int(round(lookup[action_type]))
    return 0

def parse_action_value(items, action_type):
    """Same deduplication for action_values."""
    if not items:
        return None
    lookup = {i.get("action_type"): float(i.get("value", 0)) for i in items}
    omni_key = f"omni_{action_type}"
    if omni_key in lookup:
        return round(lookup[omni_key], 2)
    if action_type in lookup:
        return round(lookup[action_type], 2)
    return None

# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_meta_insights(date_str: str):
    url = f"{META_API_BASE}/{META_AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "campaign_name,spend,impressions,inline_link_clicks,cpm,cpc,ctr,purchase_roas,actions,action_values",
        "time_range": json.dumps({"since": date_str, "until": date_str}),
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
        spend       = float(row.get("spend", 0))

        # Skip campaigns with less than $1 spend
        if spend < MIN_SPEND:
            continue

        actions     = row.get("actions", [])
        action_vals = row.get("action_values", [])

        campaigns.append({
            "name":        row.get("campaign_name", "Unknown Campaign"),
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

# ── Format ─────────────────────────────────────────────────────────────────────

def roas_emoji(roas):
    if roas is None:   return "📊"
    if roas >= 3:      return "🟢"
    if roas >= 1.5:    return "🟡"
    return "🔴"

def fmt_roas(v): return f"{v:.2f}x"  if v is not None else "N/A"
def fmt_ov(v):   return f"${v:,.2f}" if v is not None else "N/A"

def format_slack_message(campaigns, totals, report_date):
    if campaigns is None:
        return {
            "text": f"📊 Meta Ads Daily Report — {report_date}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"*📊 Meta Ads Daily Report — {report_date}*\n\n_No spend data found for this date._"}}]
        }

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text",
                     "text": f"{roas_emoji(totals['roas'])}  Meta Ads Daily Report", "emoji": True}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": f"📅  *{report_date}*  ·  _Campaigns with $1+ spend only_"}]
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
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                "text": "Automated report · Meta Ads API · Delivered 08:00 UK time"}]
        }
    ]

    return {"text": f"Meta Ads Daily Report — {report_date}", "blocks": blocks}

# ── Post to Slack ──────────────────────────────────────────────────────────────

def post_to_slack(payload):
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15
    )
    resp.raise_for_status()
    print(f"✅ Posted to Slack (status {resp.status_code})")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not all([META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, SLACK_WEBHOOK_URL]):
        raise EnvironmentError("Missing env vars: META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, SLACK_WEBHOOK_URL")

    report_date = get_yesterday()
    print(f"Fetching Meta Ads insights for {report_date}...")

    campaigns, totals = fetch_meta_insights(report_date)
    payload = format_slack_message(campaigns, totals, report_date)

    print("Posting to Slack...")
    post_to_slack(payload)

if __name__ == "__main__":
    main()