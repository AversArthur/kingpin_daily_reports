"""
Shared Meta Ads API client and data processing.
Used by email_reporter.py.

.env variables:
    META_ACCESS_TOKEN   -- Long-lived Meta access token (ads_read scope)
    META_AD_ACCOUNT_ID  -- e.g. act_785368271166897
"""

import json
import requests

META_API_VERSION = "v19.0"
META_API_BASE = f"https://graph.facebook.com/{META_API_VERSION}"
MIN_SPEND = 1.0


def parse_roas(row):
    """Pick omni_purchase ROAS first, fall back to purchase."""
    roas_raw = row.get("purchase_roas", [])
    omni = next(
        (float(i["value"]) for i in roas_raw
         if i.get("action_type") == "omni_purchase"),
        None,
    )
    if omni is not None:
        return omni
    return next(
        (float(i["value"]) for i in roas_raw
         if i.get("action_type") == "purchase"),
        None,
    )


def parse_action(items, action_type):
    """Prefer omni_ version of an action type; never double-count."""
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
    """Same deduplication logic for action_values."""
    if not items:
        return None
    lookup = {i.get("action_type"): float(i.get("value", 0)) for i in items}
    omni_key = f"omni_{action_type}"
    if omni_key in lookup:
        return round(lookup[omni_key], 2)
    if action_type in lookup:
        return round(lookup[action_type], 2)
    return None


def fetch_meta_insights(since, until, access_token, ad_account_id):
    """
    Fetch campaign-level insights from Meta Ads API.
    Returns (campaigns, totals) or (None, None) if no data.

    CTR is link click-through rate (inline_link_clicks / impressions),
    NOT CTR All. CPM and CPC totals are derived from raw totals, not averages.
    """
    url = f"{META_API_BASE}/{ad_account_id}/insights"
    params = {
        "access_token": access_token,
        "fields": (
            "campaign_name,spend,impressions,inline_link_clicks,"
            "cpm,purchase_roas,actions,action_values"
        ),
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
        if spend < MIN_SPEND:
            continue

        impressions = int(row.get("impressions", 0))
        clicks = int(row.get("inline_link_clicks", 0))
        actions = row.get("actions", [])
        action_vals = row.get("action_values", [])

        # Link CTR and link CPC — both derived from inline_link_clicks only
        link_ctr = (clicks / impressions * 100) if impressions > 0 else 0.0
        link_cpc = (spend / clicks) if clicks > 0 else 0.0

        # CPM from API is correct (spend / impressions * 1000)
        campaigns.append({
            "name": row.get("campaign_name", "Unknown"),
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "landing_page_views": parse_action(actions, "landing_page_view"),
            "cpm": float(row.get("cpm", 0)),
            "cpc": link_cpc,
            "ctr": link_ctr,
            "roas": parse_roas(row),
            "carts": parse_action(actions, "add_to_cart"),
            "checkouts": parse_action(actions, "initiate_checkout"),
            "orders": parse_action(actions, "purchase"),
            "order_value": parse_action_value(action_vals, "purchase"),
        })

    if not campaigns:
        return None, None

    campaigns.sort(key=lambda x: x["spend"], reverse=True)

    total_spend = sum(c["spend"] for c in campaigns)
    total_impressions = sum(c["impressions"] for c in campaigns)
    total_clicks = sum(c["clicks"] for c in campaigns)

    # Correct aggregate formulas (not weighted averages)
    total_cpm = (total_spend / total_impressions * 1000) \
        if total_impressions else 0.0
    total_cpc = (total_spend / total_clicks) if total_clicks else 0.0
    total_ctr = (total_clicks / total_impressions * 100) \
        if total_impressions else 0.0

    rc = [c for c in campaigns if c["roas"] is not None]
    roas_total = None
    if rc:
        rs = sum(c["spend"] for c in rc)
        roas_total = (
            sum(c["roas"] * c["spend"] for c in rc) / rs if rs else None
        )

    ov_vals = [
        c["order_value"] for c in campaigns if c["order_value"] is not None
    ]

    totals = {
        "spend": total_spend,
        "impressions": total_impressions,
        "clicks": total_clicks,
        "landing_page_views": sum(c["landing_page_views"] for c in campaigns),
        "cpm": total_cpm,
        "cpc": total_cpc,
        "ctr": total_ctr,
        "roas": roas_total,
        "carts": sum(c["carts"] for c in campaigns),
        "checkouts": sum(c["checkouts"] for c in campaigns),
        "orders": sum(c["orders"] for c in campaigns),
        "order_value": round(sum(ov_vals), 2) if ov_vals else None,
    }

    return campaigns, totals


def fmt_currency(v):
    return f"${v:,.2f}" if v is not None else "N/A"
