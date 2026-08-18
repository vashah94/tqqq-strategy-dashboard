#!/usr/bin/env python3
"""
notify_email.py

Reads docs/data/signals.json (already produced by generate_dashboard_data.py)
and emails a notification if either strategy's signal changed since the last
run. This is a notification layer only — it does not touch strategy logic,
and it does not recompute anything; it just reads the "changed" flags that
generate_dashboard_data.py already derived from run_v10()/run_v2().

Required environment variables:
  EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD

Optional:
  SMTP_HOST     (default: smtp.gmail.com)
  SMTP_PORT     (default: 587)
  FORCE_EMAIL=1 (send a status email even if nothing changed — for testing)

If EMAIL_FROM/EMAIL_TO/EMAIL_PASSWORD aren't set, this exits quietly (0) so
the workflow doesn't fail just because email hasn't been configured yet.
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "docs" / "data" / "signals.json"
STATE_COLOR = {2: "#16a34a", 1: "#2563eb", 0: "#6b7280"}


def build_email(data: dict, changed_ids: list[str], forced: bool) -> tuple[str, str]:
    strategies = data["strategies"]
    if forced:
        subject = f"TQQQ dashboard test email — {data['as_of_date']}"
    elif len(changed_ids) == 1:
        s = strategies[changed_ids[0]]
        subject = f"TQQQ {s['id']} signal changed: {s['prev_state_name']} -> {s['state_name']}"
    else:
        subject = f"TQQQ signals changed ({', '.join(changed_ids)}) — {data['as_of_date']}"

    sections = []
    for sid in changed_ids:
        s = strategies[sid]
        color = STATE_COLOR[s["state"]]
        changed_line = (
            f"Was <b>{s['prev_state_name']}</b> &rarr; now "
            f"<b style=\"color:{color};\">{s['state_name']}</b>"
            if s.get("changed") else
            f"No change &mdash; currently <b style=\"color:{color};\">{s['state_name']}</b>"
        )
        action_line = (
            f"Action for next open: sell {s['prev_state_name']}, buy <b>{s['state_ticker']}</b>."
            if s.get("changed") else
            "No trade needed."
        )
        sections.append(f"""
        <div style="background:#f8fafc;border-radius:8px;padding:16px;margin-bottom:16px;">
          <div style="font-size:12px;color:#64748b;text-transform:uppercase;letter-spacing:1px;">{s['name']}</div>
          <div style="font-size:15px;margin-top:6px;">{changed_line}</div>
          <div style="font-size:13px;color:#475569;margin-top:6px;">{action_line}</div>
        </div>""")

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
    <body style="font-family:'Segoe UI',Arial,sans-serif;background:#f1f5f9;margin:0;padding:20px;">
      <div style="max-width:560px;margin:auto;background:#fff;border-radius:12px;overflow:hidden;">
        <div style="background:#0f172a;padding:20px 24px;">
          <div style="color:#f8fafc;font-size:20px;font-weight:800;">TQQQ Strategy Signal Change</div>
          <div style="color:#94a3b8;font-size:12px;margin-top:4px;">
            {data['as_of_date']} &middot; generated {data['generated_at']}
          </div>
        </div>
        <div style="padding:20px 24px;">
          {''.join(sections)}
          <div style="font-size:11px;color:#94a3b8;border-top:1px solid #e2e8f0;padding-top:12px;">
            Full dashboard (thresholds, bodyguard status, chart) is on your GitHub Pages site.
          </div>
        </div>
      </div>
    </body></html>"""
    return subject, html


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
        print("Email not configured (EMAIL_FROM/EMAIL_TO/EMAIL_PASSWORD) — skipping notification.")
        return 0

    if not DATA_PATH.exists():
        print(f"{DATA_PATH} not found — run generate_dashboard_data.py first.", file=sys.stderr)
        return 1

    data = json.loads(DATA_PATH.read_text())
    changed_ids = [sid for sid, s in data["strategies"].items() if s.get("changed")]
    forced = os.getenv("FORCE_EMAIL") == "1"

    if not changed_ids and not forced:
        print("No signal changes — no email sent.")
        return 0

    real_change = bool(changed_ids)
    if not changed_ids and forced:
        changed_ids = list(data["strategies"].keys())

    cfg = {
        "from": ef,
        "to": et,
        "password": ep,
        "host": os.getenv("SMTP_HOST") or "smtp.gmail.com",
        "port": int(os.getenv("SMTP_PORT") or "587"),
    }
    subject, html = build_email(data, changed_ids, forced=forced and not real_change)
    send(subject, html, cfg)
    print(f"Email sent to {et}: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
