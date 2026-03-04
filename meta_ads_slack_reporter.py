"""
Meta Ads → Slack Daily Reporter
================================
Pulls yesterday's Meta Ads data per campaign:
  Performance: Spend, CPM, CPC, CTR, ROAS
  Conversions: Carts, Checkouts, Orders, Order Value

Schedule at 08:00 UK time (UTC in winter, BST=UTC+1 in summer).

Requirements:
    pip install requests python-dotenv

.env variables:
    META_ACCESS_TOKEN   — Long-lived Meta access token (ads_read scope)
    META_AD_ACCOUNT_ID  — e.g. act_785368271166897
    SLACK_WEBHOOK_URL   — Incoming webhook for your Slack channel
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

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_yesterday():
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

def parse_roas(row):
    for item in row.get("purchase_roas", []):
        if item.get("action_type") in ("omni_purchase", "purchase"):
            return float(item["value"])
    return None

def parse_action(actions, *types):
    """Sum action counts for given action_types."""
    total = sum(float(i.get("value", 0)) for i in (actions or []) if i.get("action_type") in types)
    return int(round(total))

def parse_action_value(action_values, *types):
    """Sum action values for given action_types."""
    vals = [float(i.get("value", 0)) for i in (action_values or []) if i.get("action_type") in types]
    return round(sum(vals), 2) if vals else None

# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_meta_insights(date_str: str):
    url = f"{META_API_BASE}/{META_AD_ACCOUNT_ID}/insights"
    params = {
        "access_token": META_ACCESS_TOKEN,
        "fields": "campaign_name,spend,cpm,cpc,ctr,purchase_roas,actions,action_values",
        "time_range": json.dumps({"since": date_str, "until": date_str}),
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
        spend        = float(row.get("spend", 0))
        actions      = row.get("actions", [])
        action_vals  = row.get("action_values", [])
        campaigns.append({
            "name":        row.get("campaign_name", "Unknown Campaign"),
            "spend":       spend,
            "cpm":         float(row.get("cpm", 0)),
            "cpc":         float(row.get("cpc", 0)),
            "ctr":         float(row.get("ctr", 0)),
            "roas":        parse_roas(row),
            "carts":       parse_action(actions,     "add_to_cart",        "omni_add_to_cart"),
            "checkouts":   parse_action(actions,     "initiate_checkout",  "omni_initiated_checkout"),
            "orders":      parse_action(actions,     "purchase",           "omni_purchase"),
            "order_value": parse_action_value(action_vals, "purchase",     "omni_purchase"),
        })

    campaigns.sort(key=lambda x: x["spend"], reverse=True)

    # Weighted averages for rate metrics
    total_spend = sum(c["spend"] for c in campaigns)
    def wavg(key):
        return sum(c[key] * c["spend"] for c in campaigns) / total_spend if total_spend else 0

    # Weighted ROAS across campaigns that have it
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

# ── Format ─────────────────────────────────────────────────────────────────────

def roas_emoji(roas):
    if roas is None:   return "📊"
    if roas >= 3:      return "🟢"
    if roas >= 1.5:    return "🟡"
    return "🔴"

def fmt_roas(v):   return f"{v:.2f}x"   if v is not None else "N/A"
def fmt_ov(v):     return f"${v:,.2f}"  if v is not None else "N/A"

def campaign_block(i, c):
    roas_str = fmt_roas(c["roas"])
    ov_str   = fmt_ov(c["order_value"])
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*{i}. {c['name']}*\n"
                f"> 💸 Spend: `${c['spend']:,.2f}`  ·  "
                f"📦 CPM: `${c['cpm']:,.2f}`  ·  "
                f"🖱 CPC: `${c['cpc']:,.2f}`  ·  "
                f"👆 CTR: `{c['ctr']:.2f}%`  ·  "
                f"💰 ROAS: `{roas_str}`\n"
                f"> 🛒 Carts: `{c['carts']}`  ·  "
                f"🏁 Checkouts: `{c['checkouts']}`  ·  "
                f"📦 Orders: `{c['orders']}`  ·  "
                f"💵 Order Value: `{ov_str}`"
            )
        }
    }

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
            "elements": [{"type": "mrkdwn", "text": f"📅  *{report_date}*"}]
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*📣 Liquid Collagen Stix - Campaign Breakdown*"}},
    ]

    for i, c in enumerate(campaigns, 1):
        blocks.append(campaign_block(i, c))

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

# ── Post ───────────────────────────────────────────────────────────────────────

def post_to_slack(payload):
    resp = requests.post(SLACK_WEBHOOK_URL, json=payload,
                         headers={"Content-Type": "application/json"}, timeout=15)
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