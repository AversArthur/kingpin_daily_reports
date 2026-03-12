"""
Meta Ads → Email Reporter
==========================
Modes:
    python email_reporter.py           # weekly: daily + full-week, all recipients
    python email_reporter.py --daily   # daily only, all recipients
    python email_reporter.py --test    # weekly, test recipient only
    python email_reporter.py --daily --test  # daily, test recipient only

Schedules (GitHub Actions):
    Daily  — every day 08:00 UK time
    Weekly — every Friday 08:00 UK time

.env variables:
    META_ACCESS_TOKEN   — Long-lived Meta access token
    META_AD_ACCOUNT_ID  — e.g. act_785368271166897
    GMAIL_USER          — sender Gmail address (aversartur@gmail.com)
    GMAIL_APP_PASSWORD  — Gmail app password (not your regular password)
"""

import os
import sys
import smtplib
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

from meta_ads import fetch_meta_insights, fmt_currency

load_dotenv()

META_ACCESS_TOKEN  = os.getenv("META_ACCESS_TOKEN")
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID")
GMAIL_USER         = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

RECIPIENTS = [
    "Kristin@fridaycollective.co",
    "evgeny.nikulitsa@gmail.com",
    "neagling@proxcapitalgroup.com",
    "aversartur@gmail.com",
]

TEST_RECIPIENT = "jamaly777@gmail.com"


# ── Date helpers ────────────────────────────────────────────────────────────────

def get_date_ranges():
    today     = date.today()
    yesterday = today - timedelta(days=1)
    last_fri  = today - timedelta(days=7)
    return (
        yesterday.strftime("%Y-%m-%d"),
        yesterday.strftime("%d %b %Y"),
        last_fri.strftime("%Y-%m-%d"),
        yesterday.strftime("%Y-%m-%d"),
        f"{last_fri.strftime('%d %b')} \u2013 {yesterday.strftime('%d %b %Y')}",
    )


# ── HTML helpers ────────────────────────────────────────────────────────────────

def roas_color(roas):
    if roas is None: return "#888888"
    if roas >= 3:    return "#22c55e"
    if roas >= 1.5:  return "#f59e0b"
    return "#ef4444"


def _summary_cell(label, value, color="#111"):
    return (
        f'<td style="padding:14px 16px;text-align:center">'
        f'<div style="font-size:11px;color:#888;text-transform:uppercase;'
        f'letter-spacing:.5px">{label}</div>'
        f'<div style="font-size:18px;font-weight:700;color:{color};'
        f'margin-top:2px">{value}</div></td>'
    )


def section_html(campaigns, totals, label, period_type):
    if campaigns is None:
        return "<p style='color:#888'>No spend data for this period.</p>"

    roas_val  = totals["roas"]
    dot_color = roas_color(roas_val)
    roas_str  = f"{roas_val:.2f}x" if roas_val else "N/A"
    icon      = "\U0001f4c5" if period_type == "daily" else "\U0001f4c6"
    heading   = "Daily Report \u2014 Yesterday" if period_type == "daily" else "Weekly Report"

    td = 'style="padding:8px 6px;text-align:right"'
    td_num = 'style="padding:8px 6px;color:#888"'
    td_name = 'style="padding:8px 6px;font-weight:600;color:#111;max-width:180px"'

    rows = ""
    for i, c in enumerate(campaigns, 1):
        bg = "#ffffff" if i % 2 == 1 else "#f9f9f9"
        r = c["roas"]
        roas_cell = (
            f'<td {td} style="padding:8px 6px;text-align:right;'
            f'color:{roas_color(r)};font-weight:700">'
            f'{f"{r:.2f}x" if r else "N/A"}</td>'
        )
        rows += (
            f'<tr style="background:{bg}">'
            f'<td {td_num}>{i}</td>'
            f'<td {td_name}>{c["name"]}</td>'
            f'<td {td}>{fmt_currency(c["spend"])}</td>'
            f'{roas_cell}'
            f'<td {td}>{c["clicks"]}</td>'
            f'<td {td}>{c["landing_page_views"]}</td>'
            f'<td {td}>{fmt_currency(c["cpm"])}</td>'
            f'<td {td}>{fmt_currency(c["cpc"])}</td>'
            f'<td {td}>{c["ctr"]:.2f}%</td>'
            f'<td {td}>{c["carts"]}</td>'
            f'<td {td}>{c["checkouts"]}</td>'
            f'<td {td}>{c["orders"]}</td>'
            f'<td {td}>{fmt_currency(c["order_value"])}</td>'
            f'</tr>'
        )

    td_tot = 'style="padding:8px 6px;text-align:right"'
    total_row = (
        f'<tr style="background:#111;color:#fff;font-weight:700">'
        f'<td style="padding:8px 6px"></td>'
        f'<td style="padding:8px 6px">TOTAL</td>'
        f'<td {td_tot}>{fmt_currency(totals["spend"])}</td>'
        f'<td style="padding:8px 6px;text-align:right;'
        f'color:{roas_color(roas_val)}">{roas_str}</td>'
        f'<td {td_tot}>{totals["clicks"]}</td>'
        f'<td {td_tot}>{totals["landing_page_views"]}</td>'
        f'<td {td_tot}>{fmt_currency(totals["cpm"])}</td>'
        f'<td {td_tot}>{fmt_currency(totals["cpc"])}</td>'
        f'<td {td_tot}>{totals["ctr"]:.2f}%</td>'
        f'<td {td_tot}>{totals["carts"]}</td>'
        f'<td {td_tot}>{totals["checkouts"]}</td>'
        f'<td {td_tot}>{totals["orders"]}</td>'
        f'<td {td_tot}>{fmt_currency(totals["order_value"])}</td>'
        f'</tr>'
    )

    summary_row1 = (
        _summary_cell("Spend", fmt_currency(totals["spend"]))
        + _summary_cell("ROAS", roas_str, roas_color(roas_val))
        + _summary_cell("Clicks", str(totals["clicks"]))
        + _summary_cell("LP Views", str(totals["landing_page_views"]))
        + _summary_cell("Orders", str(totals["orders"]))
        + _summary_cell("Order Value", fmt_currency(totals["order_value"]))
    )
    summary_row2 = (
        _summary_cell("CTR (link)", f"{totals['ctr']:.2f}%")
        + _summary_cell("CPC", fmt_currency(totals["cpc"]))
        + _summary_cell("CPM", fmt_currency(totals["cpm"]))
        + _summary_cell("Carts", str(totals["carts"]))
        + _summary_cell("Checkouts", str(totals["checkouts"]))
        + '<td></td>'
    )

    th_s = 'style="padding:8px 6px;text-align:right;background:#111;color:#fff"'
    th_l = 'style="padding:8px 6px;text-align:left;background:#111;color:#fff"'
    th = (
        f'<th style="padding:8px 6px;text-align:left;background:#111;'
        f'color:#fff;border-radius:6px 0 0 0">#</th>'
        f'<th {th_l}>Campaign</th>'
        f'<th {th_s}>Spend</th>'
        f'<th {th_s}>ROAS</th>'
        f'<th {th_s}>Clicks</th>'
        f'<th {th_s}>LP Views</th>'
        f'<th {th_s}>CPM</th>'
        f'<th {th_s}>CPC</th>'
        f'<th {th_s}>CTR (link)</th>'
        f'<th {th_s}>Carts</th>'
        f'<th {th_s}>Checkouts</th>'
        f'<th {th_s}>Orders</th>'
        f'<th style="padding:8px 6px;text-align:right;background:#111;'
        f'color:#fff;border-radius:0 6px 0 0">Order Value</th>'
    )

    return f"""
    <div style="margin-bottom:40px">
      <h2 style="margin:0 0 4px;font-size:20px;color:#111">
        <span style="display:inline-block;width:10px;height:10px;border-radius:50%;
          background:{dot_color};margin-right:8px"></span>
        {icon} {heading}
      </h2>
      <p style="margin:0 0 20px;color:#666;font-size:13px">
        {label} &nbsp;&middot;&nbsp; Campaigns with $1+ spend only
      </p>
      <table width="100%" cellpadding="0" cellspacing="0"
        style="background:#f8f8f8;border-radius:8px;margin-bottom:24px">
        <tr>{summary_row1}</tr>
        <tr>{summary_row2}</tr>
      </table>
      <table width="100%" cellpadding="0" cellspacing="0"
        style="border-collapse:collapse;font-size:13px">
        <thead>
          <tr style="background:#111;color:#fff">{th}</tr>
        </thead>
        <tbody>{rows}{total_row}</tbody>
      </table>
    </div>
    """


def build_html_email(daily_campaigns, daily_totals, daily_label,
                     report_date,
                     weekly_campaigns=None, weekly_totals=None,
                     weekly_label=None):
    daily_sec = section_html(daily_campaigns, daily_totals, daily_label, "daily")

    weekly_block = ""
    if weekly_campaigns is not None:
        weekly_sec = section_html(
            weekly_campaigns, weekly_totals, weekly_label, "weekly"
        )
        weekly_block = (
            '<hr style="border:none;border-top:1px solid #eee;margin:32px 0">'
            + weekly_sec
        )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f0f0f0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0"
    style="background:#f0f0f0;padding:32px 0">
    <tr><td align="center">
      <table width="900" cellpadding="0" cellspacing="0"
        style="background:#fff;border-radius:12px;overflow:hidden;
        box-shadow:0 2px 12px rgba(0,0,0,.08)">
        <tr>
          <td style="background:#111;padding:28px 32px">
            <h1 style="margin:0;color:#fff;font-size:22px;font-weight:700">
              &#128202; Meta Ads Report
            </h1>
            <p style="margin:6px 0 0;color:#aaa;font-size:13px">
              Generated on {report_date} &nbsp;&middot;&nbsp; Liquid Collagen Stix
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:32px">
            {daily_sec}
            {weekly_block}
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;padding:16px 32px;border-top:1px solid #eee">
            <p style="margin:0;font-size:12px;color:#aaa">
              Meta Ads API &nbsp;&middot;&nbsp; 08:00 UK time
              &nbsp;&middot;&nbsp; Liquid Collagen Stix
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── Send email ──────────────────────────────────────────────────────────────────

# Fixed subjects and thread anchors — daily and weekly each stay in own thread.
DAILY_SUBJECT = "Meta Ads Daily Report \u2014 Liquid Collagen Stix"
DAILY_THREAD_ID = "<meta-ads-daily-report@liquid-collagen-stix>"

WEEKLY_SUBJECT = "Meta Ads Weekly Report \u2014 Liquid Collagen Stix"
WEEKLY_THREAD_ID = "<meta-ads-weekly-report@liquid-collagen-stix>"


def send_email(html_body, recipients, daily_mode=False):
    subject = DAILY_SUBJECT if daily_mode else WEEKLY_SUBJECT
    thread_id = DAILY_THREAD_ID if daily_mode else WEEKLY_THREAD_ID

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Meta Ads Reporter <{GMAIL_USER}>"
    msg["To"] = ", ".join(recipients)
    msg["In-Reply-To"] = thread_id
    msg["References"] = thread_id
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())

    print(f"Email sent to: {', '.join(recipients)}")


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    if not all([META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, GMAIL_USER, GMAIL_APP_PASSWORD]):
        raise EnvironmentError(
            "Missing env vars: META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, "
            "GMAIL_USER, GMAIL_APP_PASSWORD"
        )

    daily_mode = "--daily" in sys.argv
    test_mode  = "--test" in sys.argv
    recipients = [TEST_RECIPIENT] if test_mode else RECIPIENTS

    if test_mode:
        print(f"TEST MODE — sending only to {TEST_RECIPIENT}")
    print(f"Mode: {'daily' if daily_mode else 'weekly'}")

    (daily_date, daily_label,
     weekly_since, weekly_until, weekly_label) = get_date_ranges()

    print(f"Fetching daily data: {daily_label}...")
    daily_campaigns, daily_totals = fetch_meta_insights(
        daily_date, daily_date, META_ACCESS_TOKEN, META_AD_ACCOUNT_ID
    )

    report_date = date.today().strftime("%d %b %Y")

    if daily_mode:
        html = build_html_email(
            daily_campaigns, daily_totals, daily_label, report_date
        )
    else:
        print(f"Fetching weekly data: {weekly_label}...")
        weekly_campaigns, weekly_totals = fetch_meta_insights(
            weekly_since, weekly_until, META_ACCESS_TOKEN, META_AD_ACCOUNT_ID
        )
        html = build_html_email(
            daily_campaigns, daily_totals, daily_label, report_date,
            weekly_campaigns, weekly_totals, weekly_label,
        )

    print("Sending email...")
    send_email(html, recipients, daily_mode=daily_mode)
    print("Done!")


if __name__ == "__main__":
    main()
