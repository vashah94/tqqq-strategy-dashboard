#!/usr/bin/env python3
"""
check_staleness.py

Backstop for the daily update. If docs/data/signals.json's as_of_date
isn't today (UTC date, which matches the US market's trading-day date
at the time this runs), emails an alert so a missed run doesn't go
unnoticed. Meant to run later in the day than the update workflow's own
schedules, as an independent safety net against GitHub's schedule
trigger silently not firing.

Note: this will also fire on US market holidays, when a lagging
as_of_date is expected and not actually a problem. A once-in-a-while
false alarm on a holiday is an acceptable tradeoff for catching real
misses.

Required environment variables (same as notify_email.py):
  EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD
Optional:
  SMTP_HOST (default: smtp.gmail.com), SMTP_PORT (default: 587)

If EMAIL_FROM/EMAIL_TO/EMAIL_PASSWORD aren't set, exits quietly (0).
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "docs" / "data" / "signals.json"


def send(subject: str, html: str, cfg: dict) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as s:
        s.ehlo()
        s.starttls()
        s.login(cfg["from"], cfg["password"])
        s.sendmail(cfg["from"], cfg["to"], msg.as_string())


def main() -> int:
    ef, et, ep = os.getenv("EMAIL_FROM"), os.getenv("EMAIL_TO"), os.getenv("EMAIL_PASSWORD")
    if not all([ef, et, ep]):
        print("Email not configured — skipping staleness check.")
        return 0

    today = datetime.now(timezone.utc).date().isoformat()

    if not DATA_PATH.exists():
        as_of = None
    else:
        as_of = json.loads(DATA_PATH.read_text()).get("as_of_date")

    if as_of == today:
        print(f"Data is fresh (as_of_date={as_of}). No alert needed.")
        return 0

    cfg = {
        "from": ef,
        "to": et,
        "password": ep,
        "host": os.getenv("SMTP_HOST") or "smtp.gmail.com",
        "port": int(os.getenv("SMTP_PORT") or "587"),
    }
    subject = f"TQQQ dashboard did not update today ({today})"
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
    <body style="font-family:'Segoe UI',Arial,sans-serif;background:#f1f5f9;margin:0;padding:20px;">
      <div style="max-width:560px;margin:auto;background:#fff;border-radius:12px;overflow:hidden;">
        <div style="background:#0f172a;padding:20px 24px;">
          <div style="color:#f8fafc;font-size:20px;font-weight:800;">Dashboard update missing</div>
        </div>
        <div style="padding:20px 24px;font-size:14px;color:#1e293b;">
          <p>No fresh signal data has landed for <b>{today}</b> (latest data on the site is dated
          <b>{as_of or "unknown"}</b>).</p>
          <p>Today's scheduled workflow run(s) may not have fired. If today isn't a US market
          holiday, use the <b>Run Update</b> button on the dashboard, or run:</p>
          <pre style="background:#f8fafc;border-radius:6px;padding:10px;font-size:12px;">gh workflow run update-signals.yml --repo vashah94/tqqq-strategy-dashboard</pre>
        </div>
      </div>
    </body></html>"""
    send(subject, html, cfg)
    print(f"Staleness alert sent to {et} (as_of_date={as_of}, expected {today})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
