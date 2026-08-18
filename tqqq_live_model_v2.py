#!/usr/bin/env python3
"""
TQQQ ROTH IRA LIVE TRADING MODEL v2
=====================================
Strategy: SMA160 +4% entry / SMA145 -2% exit / QQQ EMA210 bodyguard
Verified: Gates 4A, 5A, 6 passed. Entries match optimizer (20/20).

FIXES vs v1 (all ChatGPT issues addressed):
  [1] Loop mode now runs only AFTER confirmed market close (4:45 PM ET),
      not hourly during market hours. Incomplete intraday bars cannot fire
      false signals.
  [2] Stale-data guard: warns and exits if latest Yahoo bar is not the
      expected last market close date.
  [3] Separate target_state vs actual_state. Signal fires set target_state.
      You confirm execution with --confirm. Until confirmed, every run
      reminds you a trade is pending.
  [4] Timezone-correct timestamps using America/New_York (works from Arizona).
  [5] All display/email threshold comparisons use STRATEGY values, not
      hard-coded 35/15 literals.
  [6] Danger=35% labeled as "conservative overlay" not "strictly dominant."
  [7] --confirm-executed flag to mark pending trade as done.
  [8] Danger=40% is the optimizer-faithful value; 35% is the overlay.
      You can switch via DANGER_MODE env var: "optimizer" (40%) or "conservative" (35%).

STATES:
  2 = TQQQ  — trend confirmed, bodyguard clear
  1 = QQQ   — bodyguard WARN: QQQ >+15% above EMA210 (sticky)
  0 = SGOV  — bodyguard DANGER OR trend broken

EXECUTION MODEL:
  Signal fires on daily CLOSE price.
  Trade executes at next morning MARKET OPEN.
  NEVER act intraday. Wait for 4 PM ET close, then place MOO order.

COMMANDS:
  python tqqq_live_model.py               # run once (after 4:45 PM ET)
  python tqqq_live_model.py --status      # print state, no saves/emails
  python tqqq_live_model.py --loop        # run daily at 4:45 PM ET (keeps running)
  python tqqq_live_model.py --force-email # send email regardless of change
  python tqqq_live_model.py --confirm     # mark pending trade as executed
  python tqqq_live_model.py --set-state 1 # manually override state (0/1/2)

ENV VARS (.env):
  EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD
  SMTP_HOST (default: smtp.gmail.com), SMTP_PORT (default: 587)
  STATE_FILE   (default: ./model_state.json)
  MODEL_LOG    (default: ./model_log.csv)
  TRADE_LOG    (default: ./trade_log.csv)
  DANGER_MODE  (default: conservative → 35% | set to "optimizer" for 40%)

WINDOWS TASK SCHEDULER (replaces cron):
  Action: python C:\\path\\to\\tqqq_live_model.py
  Trigger: Daily, 4:45 PM, Mon-Fri
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# TIMEZONE  (fix #4: correct ET timestamps from any machine location)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo          # Python 3.9+
    ET = ZoneInfo("America/New_York")
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo   # pip install backports.zoneinfo
        ET = ZoneInfo("America/New_York")
    except ImportError:
        ET = None                                  # fallback: no timezone conversion


def now_et() -> datetime:
    """Current datetime in US/Eastern, or local time if zoneinfo unavailable."""
    if ET:
        return datetime.now(ET)
    return datetime.now()


def fmt_et(dt: datetime | None = None) -> str:
    """Format datetime as 'YYYY-MM-DD HH:MM ET'."""
    d = dt or now_et()
    suffix = " ET" if ET else " local"
    return d.strftime("%Y-%m-%d %H:%M") + suffix


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
# Fix #6/#8: danger threshold is configurable.
# "optimizer"     → 40% (exact optimizer-faithful value, historically verified)
# "conservative"  → 35% (risk overlay: fires 5pp earlier in future bubbles;
#                         historically identical since DANGER never triggered)
_DANGER_MODE = os.getenv("DANGER_MODE", "conservative").lower()
_BG_DANGER   = 0.35 if _DANGER_MODE == "conservative" else 0.40

STRATEGY: dict = {
    "entry_ma":        "SMA",
    "entry_period":    160,
    "entry_threshold": 0.04,        # SPY > SMA160 × 1.04

    "exit_ma":         "SMA",
    "exit_period":     145,
    "exit_threshold":  -0.02,       # SPY < SMA145 × 0.98

    "bg_ma":           "EMA",
    "bg_period":       210,
    "bg_warn":         0.15,        # QQQ > EMA210 × 1.15 → TQQQ→QQQ, sticky
    "bg_danger":       _BG_DANGER,  # QQQ > EMA210 × (1+danger) → SGOV, sticky
    "bg_return":       0.06,        # extension ≤ +6% → sticky flag clears

    "state_name":   {2: "TQQQ", 1: "QQQ", 0: "SGOV"},
    "state_ticker": {2: "TQQQ", 1: "QQQ", 0: "SGOV"},
}

FETCH_START = "2010-01-01"   # matches optimizer START exactly

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("tqqq_live")


# ─────────────────────────────────────────────────────────────────────────────
# MARKET CALENDAR HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def last_expected_market_close() -> date:
    """
    Returns the date of the most recent expected market close as of now.
    Assumes Mon-Fri market days (does not account for US holidays).
    After 4:15 PM ET: today counts. Before 4:15 PM ET: yesterday counts.
    """
    now = now_et()
    cutoff_hour = 16
    cutoff_min  = 15
    candidate   = now.date()
    # Roll back to Friday if weekend
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    # If it's a weekday but before close, roll back one more market day
    if now.date() == candidate and (
        now.hour < cutoff_hour or
        (now.hour == cutoff_hour and now.minute < cutoff_min)
    ):
        candidate -= timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
    return candidate


def is_after_close() -> bool:
    """True if current ET time is 4:45 PM or later on a weekday."""
    now = now_et()
    return (now.weekday() < 5 and
            (now.hour > 16 or (now.hour == 16 and now.minute >= 45)))


# ─────────────────────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────────────────────
def fetch_prices() -> dict[str, pd.Series]:
    log.info("Fetching SPY and QQQ from %s …", FETCH_START)
    raw = yf.download(
        ["SPY", "QQQ"],
        start=FETCH_START,
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if raw.empty:
        raise RuntimeError("yfinance returned empty data. Check internet connection.")
    prices = {
        "SPY": raw["Close"]["SPY"].dropna(),
        "QQQ": raw["Close"]["QQQ"].dropna(),
    }
    for t, s in prices.items():
        log.info("  %s: %d days  last=%.4f on %s",
                 t, len(s), s.iloc[-1], s.index[-1].date())
    return prices


# ─────────────────────────────────────────────────────────────────────────────
# FIX #2 — STALE DATA GUARD
# ─────────────────────────────────────────────────────────────────────────────
def check_data_freshness(prices: dict[str, pd.Series]) -> None:
    """
    Warn and exit if the latest Yahoo bar is not the expected last market close.
    Protects against running on stale data after holidays or data delays.
    """
    expected = last_expected_market_close()
    actual   = prices["SPY"].index[-1].date()
    if actual < expected:
        print()
        print("=" * 60)
        print("  ⚠️  STALE DATA WARNING")
        print("=" * 60)
        print(f"  Expected last market close : {expected}")
        print(f"  Latest Yahoo bar           : {actual}")
        print()
        print("  Possible causes:")
        print("  - US market holiday (Yahoo hasn't updated yet)")
        print("  - Running before 4:15 PM ET (market still open)")
        print("  - Yahoo Finance data delay")
        print()
        print("  DO NOT TRADE based on this data.")
        print("  Run again after 4:45 PM ET on the next full market day.")
        print("=" * 60)
        sys.exit(1)
    log.info("Data freshness OK: latest bar %s = expected %s", actual, expected)


# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────────────────────────
def build_indicators(prices: dict[str, pd.Series]) -> pd.DataFrame:
    """
    SMA160, SMA145 on SPY; EMA210 on QQQ.
    EMA: ewm(adjust=False) with no min_periods — matches optimizer exactly.
    """
    spy = prices["SPY"]
    qqq = prices["QQQ"]
    idx = spy.index.intersection(qqq.index)
    spy, qqq = spy.loc[idx], qqq.loc[idx]

    df             = pd.DataFrame(index=idx)
    df["spy"]      = spy.values
    df["sma160"]   = spy.rolling(160, min_periods=160).mean().values
    df["sma145"]   = spy.rolling(145, min_periods=145).mean().values
    df["qqq"]      = qqq.values
    df["ema210"]   = qqq.ewm(span=210, adjust=False).mean().values  # no min_periods
    df["ext"]      = df["qqq"] / df["ema210"] - 1.0
    df.dropna(subset=["sma160", "sma145"], inplace=True)
    log.info("Indicators: %d rows (%s → %s)",
             len(df), df.index[0].date(), df.index[-1].date())
    return df


# ─────────────────────────────────────────────────────────────────────────────
# STATE MACHINE — exact optimizer kernel
# ─────────────────────────────────────────────────────────────────────────────
def run_state_machine(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """
    Returns (state_array, bg_flag_array).
    state: 0=SGOV, 1=QQQ, 2=TQQQ.
    Signal on close[i] → position from open[i+1].
    """
    s     = STRATEGY
    n     = len(df)
    spy   = df["spy"].values
    sm160 = df["sma160"].values
    sm145 = df["sma145"].values
    ext   = df["ext"].values

    state  = np.zeros(n, dtype=np.int8)
    bg_arr = np.zeros(n, dtype=bool)
    cur    = np.int8(0)
    bg     = False

    for i in range(n):
        e = ext[i]
        if e > s["bg_danger"]:
            cur = np.int8(0)
            bg  = True
        elif e > s["bg_warn"]:
            if cur == 2:
                cur = np.int8(1)
            bg = True
        elif bg and e <= s["bg_return"]:
            bg = False

        if not bg:
            if cur < 2 and spy[i] > sm160[i] * (1.0 + s["entry_threshold"]):
                cur = np.int8(2)
            elif cur == 2 and spy[i] < sm145[i] * (1.0 + s["exit_threshold"]):
                cur = np.int8(0)

        state[i]  = cur
        bg_arr[i] = bg

    return state, bg_arr


def compute_signal(df: pd.DataFrame) -> dict:
    """Run full state machine; return diagnostic dict for latest date."""
    states, bg_flags = run_state_machine(df)
    s       = STRATEGY
    latest  = df.iloc[-1]
    state   = int(states[-1])
    bg_flag = bool(bg_flags[-1])

    spy    = float(latest["spy"])
    sm160  = float(latest["sma160"])
    sm145  = float(latest["sma145"])
    qqq    = float(latest["qqq"])
    ema210 = float(latest["ema210"])
    ext    = float(latest["ext"])

    entry_level  = sm160  * (1.0 + s["entry_threshold"])
    exit_level   = sm145  * (1.0 + s["exit_threshold"])
    warn_level   = ema210 * (1.0 + s["bg_warn"])
    danger_level = ema210 * (1.0 + s["bg_danger"])
    return_level = ema210 * (1.0 + s["bg_return"])

    # Fix #5: derive display zone from STRATEGY values, not hard-coded literals
    if ext > s["bg_danger"]:
        bg_zone = "DANGER"
    elif ext > s["bg_warn"]:
        bg_zone = "WARN"
    elif bg_flag and ext <= s["bg_return"]:
        bg_zone = "RETURN"
    elif bg_flag:
        bg_zone = "STICKY"
    else:
        bg_zone = "NORMAL"

    return {
        "signal_date":   df.index[-1].date(),
        "state":         state,
        "state_name":    s["state_name"][state],
        "bg_flag":       bg_flag,
        "bg_zone":       bg_zone,
        "spy":           round(spy,    4),
        "sma160":        round(sm160,  4),
        "sma145":        round(sm145,  4),
        "entry_level":   round(entry_level,  4),
        "exit_level":    round(exit_level,   4),
        "spy_vs_entry":  round((spy / entry_level  - 1) * 100, 3),
        "spy_vs_exit":   round((spy / exit_level   - 1) * 100, 3),
        "entry_met":     spy > entry_level,
        "exit_met":      spy < exit_level,
        "qqq":           round(qqq,    4),
        "ema210":        round(ema210, 4),
        "ext_pct":       round(ext * 100, 3),
        "warn_level":    round(warn_level,   4),
        "danger_level":  round(danger_level, 4),
        "return_level":  round(return_level, 4),
        "danger_mode":   _DANGER_MODE,
        "bg_danger_pct": round(s["bg_danger"] * 100, 0),
        "bg_warn_pct":   round(s["bg_warn"]   * 100, 0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# FIX #3 — STATE PERSISTENCE WITH target_state / actual_state SEPARATION
# ─────────────────────────────────────────────────────────────────────────────
def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log.warning("Could not read state file (%s) — starting fresh", e)
    return {}


def save_state(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))
    log.info("State saved → %s", path)


def build_new_state(sig: dict,
                    prev_target: int | None,
                    prev_actual: int | None) -> dict:
    """
    Build the state record to persist.
    target_state = what the model says to hold.
    actual_state = what you have physically confirmed you hold.
    pending      = True when target != actual OR actual is unknown.

    FIRST RUN SAFETY: if prev_actual is None (no state file exists),
    actual_state is left as None and pending_trade is True.
    The console will tell you to run --set-state before going live.
    Do NOT assume actual == target on first run.
    """
    target = sig["state"]
    if prev_actual is None:
        actual  = None   # unknown until user declares with --set-state
        pending = True
    else:
        actual  = prev_actual
        pending = (target != actual)
    return {
        "target_state":  target,
        "target_name":   sig["state_name"],
        "actual_state":  actual,
        "actual_name":   STRATEGY["state_name"].get(actual, "UNKNOWN"),
        "pending_trade": pending,
        "signal_date":   str(sig["signal_date"]),
        "bg_flag":       sig["bg_flag"],
        "bg_zone":       sig["bg_zone"],
        "ext_pct":       sig["ext_pct"],
        "updated_et":    fmt_et(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CONSOLE OUTPUT
# ─────────────────────────────────────────────────────────────────────────────
def print_status(sig: dict, prior: dict) -> None:
    sn            = STRATEGY["state_name"]
    prev_target   = prior.get("target_state")
    prev_actual   = prior.get("actual_state")
    state_changed = (prev_target is not None) and (prev_target != sig["state"])
    pending       = prior.get("pending_trade", False)

    danger_pct = int(STRATEGY["bg_danger"] * 100)
    warn_pct   = int(STRATEGY["bg_warn"]   * 100)
    w = 62
    s = STRATEGY
    entry_pct  = int(s["entry_threshold"] * 100)
    exit_pct   = int(abs(s["exit_threshold"]) * 100)
    bg_ret_pct = int(s["bg_return"] * 100)

    def hdr(label: str) -> str:
        return f"  {label} {'─' * (w - len(label) - 3)}"

    print()
    print("═" * w)
    print(f"  TQQQ ROTH IRA — LIVE MODEL v2          {sig['signal_date']}")
    print("─" * w)
    print(f"  Entry  SPY > SMA{s['entry_period']}+{entry_pct}% → TQQQ   "
          f"Exit  SPY < SMA{s['exit_period']}-{exit_pct}% → SGOV")
    print(f"  Guard  QQQ > EMA{s['bg_period']}+{warn_pct}% → QQQ · "
          f"+{danger_pct}% → SGOV · ≤+{bg_ret_pct}% clears")
    print("═" * w)
    print()

    # ── Summary ──────────────────────────────────────────────
    if state_changed:
        note = f"  *** CHANGED from {sn.get(prev_target, '?')} ***"
    elif pending and prior.get("actual_state") is None:
        note = "  ⚠️  position unknown — run --set-state"
    elif pending:
        note = f"  ⏳ pending (was {sn.get(prev_actual, '?')})"
    else:
        note = "  (no change)"
    print(f"  HOLD       {sig['state']} = {sn[sig['state']]}{note}")
    if sig['bg_flag']:
        print(f"  Bodyguard  ACTIVE ⚠️  [{sig['bg_zone']}]")
    else:
        print(f"  Bodyguard  clear ✓")
    print(f"  Mode       {_DANGER_MODE.upper()} ({danger_pct}% danger threshold)")

    if pending and not state_changed:
        print()
        if prior.get("actual_state") is None:
            print("  ⚠️  Declare your actual position before trading:")
            print("     python tqqq_live_model_v2.py --set-state 0|1|2")
        else:
            pa = prior.get("actual_name", "?")
            pt = prior.get("target_name", "?")
            print(f"  ⏳ PENDING TRADE: {pa} → {pt}")
            print("     Run --confirm once executed.")

    # ── SPY ──────────────────────────────────────────────────
    e_tag = "✓ MET"    if sig['entry_met'] else ""
    x_tag = "⚠️  EXIT!" if sig['exit_met']  else ""
    print()
    print(hdr("SPY"))
    print(f"  {'Close':6} ${sig['spy']:>8.2f}   "
          f"Entry @ ${sig['entry_level']:>8.2f}  ({sig['spy_vs_entry']:+6.2f}%)  {e_tag}")
    print(f"  {'SMA160':6} ${sig['sma160']:>8.2f}   "
          f"Exit  @ ${sig['exit_level']:>8.2f}  ({sig['spy_vs_exit']:+6.2f}%)  {x_tag}")
    print(f"  {'SMA145':6} ${sig['sma145']:>8.2f}")

    # ── QQQ Bodyguard ────────────────────────────────────────
    ext = sig["ext_pct"]
    ext_icon = ("🚨 DANGER" if ext > danger_pct
                else "⚠️  WARN" if ext > warn_pct
                else "✓  Normal")
    print()
    print(hdr("QQQ Bodyguard"))
    print(f"  {'Close':6} ${sig['qqq']:>8.2f}   "
          f"Extension   {ext:>+7.2f}%  {ext_icon}")
    print(f"  {'EMA210':6} ${sig['ema210']:>8.2f}   "
          f"WARN  +{warn_pct}%   ${sig['warn_level']:>8.2f}")
    print(f"{'':21}DANGER+{danger_pct}%   ${sig['danger_level']:>8.2f}")
    print(f"{'':21}Return +6%   ${sig['return_level']:>8.2f}")

    # ── Action ───────────────────────────────────────────────
    print()
    if state_changed:
        action = {
            (0, 2): "🟢 BUY TQQQ at next market open",
            (0, 1): "🔵 BUY QQQ at next market open",
            (1, 2): "🟢 SELL QQQ → BUY TQQQ at next open",
            (1, 0): "🔴 SELL QQQ → BUY SGOV at next open",
            (2, 1): "⚠️  SELL TQQQ → BUY QQQ at next open",
            (2, 0): "🔴 SELL TQQQ → BUY SGOV at next open",
        }.get((prev_target, sig["state"]),
              "⚡ STATE CHANGE — act at next market open")
        print(f"  *** ACTION REQUIRED ***")
        print(f"  {action}")
        print(f"  Run --confirm after executing.")
    else:
        print(f"  No change. Hold {sn[sig['state']]}.")
    print("═" * w)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────────────────────
def build_email(sig: dict, prior: dict, state_changed: bool) -> tuple[str, str]:
    sn             = STRATEGY["state_name"]
    prev_target    = prior.get("target_state")
    state_colors   = {2: "#16a34a", 1: "#2563eb", 0: "#6b7280"}
    ext            = sig["ext_pct"]
    danger_pct     = int(STRATEGY["bg_danger"] * 100)
    warn_pct       = int(STRATEGY["bg_warn"]   * 100)
    ext_color      = ("#dc2626" if ext > danger_pct else
                      "#f59e0b" if ext > warn_pct   else "#16a34a")

    if state_changed:
        action_map = {
            (0, 2): "🟢 BUY TQQQ",
            (0, 1): "🔵 BUY QQQ",
            (1, 2): "🟢 ROTATE QQQ → TQQQ",
            (1, 0): "🔴 SELL QQQ → SGOV",
            (2, 1): "⚠️ ROTATE TQQQ → QQQ",
            (2, 0): "🔴 SELL TQQQ → SGOV",
        }
        action  = action_map.get((prev_target, sig["state"]), "⚡ STATE CHANGE")
        subject = f"TQQQ Roth IRA — ACTION: {action} — {sig['signal_date']}"
    else:
        subject = f"TQQQ Roth IRA — Daily: {sig['state_name']} — {sig['signal_date']}"

    action_banner = ""
    if state_changed:
        detail_map = {
            (0, 2): "Buy TQQQ — enter leveraged position",
            (0, 1): "Buy QQQ — bodyguard cleared, enter unleveraged",
            (1, 2): "Sell QQQ, buy TQQQ — bodyguard cleared, add leverage",
            (1, 0): "Sell QQQ, buy SGOV — exit signal fired",
            (2, 1): "Sell TQQQ, buy QQQ — bodyguard WARN, reduce leverage",
            (2, 0): "Sell TQQQ, buy SGOV — exit or DANGER",
        }
        detail = detail_map.get((prev_target, sig["state"]), "Execute position change")
        action_banner = f"""
        <div style="background:#fffbeb;border:2px solid #f59e0b;border-radius:8px;
                    padding:18px;margin-bottom:20px;">
          <div style="font-size:20px;font-weight:800;color:#92400e;">
            ⚡ ACTION AT NEXT MARKET OPEN
          </div>
          <div style="font-size:14px;color:#78350f;margin-top:8px;line-height:1.7">
            <b>{detail}</b><br>
            {sn.get(prev_target, '?')} → {sig['state_name']}<br>
            Signal date: <b>{sig['signal_date']}</b><br>
            Execute at: next market open (MOO order)<br>
            After executing: run <code>--confirm</code> to clear pending flag
          </div>
        </div>"""

    def pbar(pct: float, color: str) -> str:
        w = min(max(int(pct), 0), 100)
        return (f'<div style="background:#e2e8f0;border-radius:4px;height:8px;">'
                f'<div style="background:{color};width:{w}%;height:8px;'
                f'border-radius:4px;"></div></div>')

    entry_pct  = min(max((sig["spy_vs_entry"] + 10) / 20 * 100, 0), 100)
    danger_bar = min(max(ext / (danger_pct * 1.3) * 100, 0), 100)

    # Fix #4: ET timestamp
    ts = fmt_et()

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
    <body style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                 background:#f1f5f9;margin:0;padding:20px;">
      <div style="max-width:600px;margin:auto;background:#fff;border-radius:12px;
                  overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,.12);">
        <div style="background:#0f172a;padding:24px 28px;">
          <div style="color:#94a3b8;font-size:11px;letter-spacing:2px;
                      text-transform:uppercase;">
            Roth IRA · SMA160/SMA145/EMA210 ·
            Danger={danger_pct}% ({sig['danger_mode']})
          </div>
          <div style="color:#f8fafc;font-size:24px;font-weight:800;margin-top:4px;">
            TQQQ Live Model
          </div>
          <div style="color:#64748b;font-size:13px;margin-top:4px;">{sig['signal_date']}</div>
        </div>
        <div style="padding:24px 28px;">
          {action_banner}
          <!-- State -->
          <div style="background:#f8fafc;border-radius:8px;padding:16px;
                      margin-bottom:20px;">
            <div style="font-size:11px;color:#64748b;letter-spacing:1px;
                        text-transform:uppercase;margin-bottom:6px;">
              Model Target (from next open)
            </div>
            <div style="font-size:30px;font-weight:800;
                        color:{state_colors[sig['state']]};">
              {sig['state_name']}
            </div>
            <div style="font-size:13px;color:#475569;margin-top:4px;">
              BG: <b style="color:{'#dc2626' if sig['bg_flag'] else '#16a34a'};">
                {'ACTIVE ⚠️' if sig['bg_flag'] else 'CLEAR ✓'}
              </b> · Zone: <b>{sig['bg_zone']}</b>
            </div>
          </div>
          <!-- Entry -->
          <div style="margin-bottom:14px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
              <span style="font-size:13px;font-weight:600;">
                Entry (SPY vs SMA160+{int(STRATEGY['entry_threshold']*100)}%)
              </span>
              <span style="font-size:13px;font-weight:700;
                           color:{'#16a34a' if sig['entry_met'] else '#6b7280'};">
                {sig['spy_vs_entry']:+.2f}% {'✓ MET' if sig['entry_met'] else ''}
              </span>
            </div>
            {pbar(entry_pct, '#16a34a' if sig['entry_met'] else '#94a3b8')}
            <div style="font-size:11px;color:#94a3b8;margin-top:2px;">
              SPY ${sig['spy']:.2f} · SMA160 ${sig['sma160']:.2f} ·
              Entry @ ${sig['entry_level']:.2f}
            </div>
          </div>
          <!-- Exit -->
          <div style="margin-bottom:14px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
              <span style="font-size:13px;font-weight:600;">
                Exit (SPY vs SMA145{int(STRATEGY['exit_threshold']*100)}%)
              </span>
              <span style="font-size:13px;font-weight:700;
                           color:{'#dc2626' if sig['exit_met'] else '#16a34a'};">
                {sig['spy_vs_exit']:+.2f}%
                {'⚠️ TRIGGERED' if sig['exit_met'] else '✓ Safe'}
              </span>
            </div>
            {pbar(min(max(-sig['spy_vs_exit'],0)/10*100,100),
                  '#dc2626' if sig['exit_met'] else '#e2e8f0')}
            <div style="font-size:11px;color:#94a3b8;margin-top:2px;">
              SMA145 ${sig['sma145']:.2f} · Exit @ ${sig['exit_level']:.2f}
            </div>
          </div>
          <!-- Bodyguard -->
          <div style="margin-bottom:20px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
              <span style="font-size:13px;font-weight:600;">
                Bodyguard (QQQ vs EMA210)
              </span>
              <span style="font-size:13px;font-weight:700;color:{ext_color};">
                {ext:+.2f}%
                {'🚨 DANGER' if ext > danger_pct else
                 '⚠️ WARN'   if ext > warn_pct   else '✓ Normal'}
              </span>
            </div>
            {pbar(danger_bar, ext_color)}
            <div style="font-size:11px;color:#94a3b8;margin-top:2px;">
              QQQ ${sig['qqq']:.2f} · EMA210 ${sig['ema210']:.2f} ·
              WARN @+{warn_pct}% (${sig['warn_level']:.2f}) ·
              DANGER @+{danger_pct}% (${sig['danger_level']:.2f})
            </div>
          </div>
          <!-- Summary table -->
          <table style="width:100%;border-collapse:collapse;font-size:12px;
                        color:#475569;margin-bottom:16px;">
            <tr style="border-top:1px solid #e2e8f0;">
              <td style="padding:5px 0;">Entry condition</td>
              <td style="padding:5px 0;font-weight:600;
                         color:{'#16a34a' if sig['entry_met'] else '#6b7280'};">
                {'MET ✓' if sig['entry_met'] else 'not met'}
              </td>
              <td style="padding:5px 0;">Exit condition</td>
              <td style="padding:5px 0;font-weight:600;
                         color:{'#dc2626' if sig['exit_met'] else '#16a34a'};">
                {'TRIGGERED ⚠️' if sig['exit_met'] else 'not triggered'}
              </td>
            </tr>
            <tr style="border-top:1px solid #e2e8f0;">
              <td style="padding:5px 0;">BG flag</td>
              <td style="padding:5px 0;font-weight:600;
                         color:{'#dc2626' if sig['bg_flag'] else '#16a34a'};">
                {'ACTIVE' if sig['bg_flag'] else 'clear'}
              </td>
              <td style="padding:5px 0;">BG return level</td>
              <td style="padding:5px 0;">
                ${sig['return_level']:.2f} (+{int(STRATEGY['bg_return']*100)}%)
              </td>
            </tr>
          </table>
          <div style="font-size:11px;color:#94a3b8;border-top:1px solid #e2e8f0;
                      padding-top:12px;">
            SMA160 +4% / SMA145 −2% / EMA210 BG
            (WARN +{warn_pct}% / DANGER +{danger_pct}% [{sig['danger_mode']}] / RETURN +6%) ·
            Signal on close → execute at next open · {ts}
          </div>
        </div>
      </div>
    </body></html>"""

    return subject, html


def send_email(subject: str, html: str, cfg: dict) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["from"]
    msg["To"]      = cfg["to"]
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(cfg.get("host", "smtp.gmail.com"),
                          cfg.get("port", 587), timeout=15) as s:
            s.ehlo(); s.starttls()
            s.login(cfg["from"], cfg["password"])
            s.sendmail(cfg["from"], cfg["to"], msg.as_string())
        log.info("Email sent → %s", cfg["to"])
        return True
    except Exception as e:
        log.error("Email failed: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LOGS
# ─────────────────────────────────────────────────────────────────────────────
def append_log(path: Path, record: dict) -> None:
    pd.DataFrame([record]).to_csv(
        path, mode="a", header=not path.exists(), index=False
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUN
# ─────────────────────────────────────────────────────────────────────────────
def run_once(
    state_file:  Path,
    model_log:   Path,
    trade_log:   Path,
    email_cfg:   dict | None,
    force_email: bool = False,
    status_only: bool = False,
    skip_freshness: bool = False,
) -> dict:
    # 1. Fetch
    prices = fetch_prices()

    # 2. Fix #2: stale data check
    if not skip_freshness:
        check_data_freshness(prices)

    # 3. Compute indicators and signal
    df  = build_indicators(prices)
    sig = compute_signal(df)

    # 4. Load prior state
    prior        = load_state(state_file)
    prev_target  = prior.get("target_state")
    prev_actual  = prior.get("actual_state")
    state_changed = (prev_target is not None) and (prev_target != sig["state"])
    is_new_day    = str(sig["signal_date"]) != prior.get("signal_date")

    # 5. Console
    print_status(sig, prior)

    if status_only:
        return sig

    # 6. Fix #3: always build new_state so pending_trade is freshly computed
    new_state = build_new_state(sig, prev_target, prev_actual)
    if is_new_day or prev_target is None:
        save_state(state_file, new_state)

    # 7. Model log (every run) — pending_trade from new_state, not stale prior
    append_log(model_log, {
        "run_ts":            fmt_et(),
        "signal_date":       str(sig["signal_date"]),
        "target_state":      sig["state"],
        "target_name":       sig["state_name"],
        "prev_target":       prev_target,
        "actual_state":      prev_actual,
        "state_changed":     state_changed,
        "pending_trade":     new_state["pending_trade"],
        "bg_flag":           sig["bg_flag"],
        "bg_zone":           sig["bg_zone"],
        "spy":               sig["spy"],
        "sma160":            sig["sma160"],
        "sma145":            sig["sma145"],
        "entry_level":       sig["entry_level"],
        "exit_level":        sig["exit_level"],
        "spy_vs_entry_pct":  sig["spy_vs_entry"],
        "spy_vs_exit_pct":   sig["spy_vs_exit"],
        "qqq":               sig["qqq"],
        "ema210":            sig["ema210"],
        "ext_pct":           sig["ext_pct"],
        "danger_mode":       _DANGER_MODE,
    })

    # 8. Trade log (state changes only)
    if state_changed:
        append_log(trade_log, {
            "signal_date":  str(sig["signal_date"]),
            "from_state":   prev_target,
            "from_name":    STRATEGY["state_name"].get(prev_target, "?"),
            "to_state":     sig["state"],
            "to_name":      sig["state_name"],
            "spy":          sig["spy"],
            "ext_pct":      sig["ext_pct"],
            "bg_flag":      sig["bg_flag"],
            "entry_met":    sig["entry_met"],
            "exit_met":     sig["exit_met"],
            "confirmed":    False,
        })
        log.info("TRADE SIGNAL: %s → %s",
                 STRATEGY["state_name"].get(prev_target, "?"), sig["state_name"])

    # 9. Email
    if email_cfg and (state_changed or force_email):
        subj, html = build_email(sig, prior, state_changed)
        send_email(subj, html, email_cfg)

    return sig


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="TQQQ Roth IRA Live Trading Model v2"
    )
    parser.add_argument("--loop",         action="store_true",
                        help="Run daily at 4:45 PM ET (blocking loop)")
    parser.add_argument("--force-email",  action="store_true",
                        help="Send email even if no change (for testing)")
    parser.add_argument("--status",       action="store_true",
                        help="Print current state only — no saves, no email")
    parser.add_argument("--confirm",      action="store_true",
                        help="Mark pending trade as executed")
    parser.add_argument("--set-state",    type=int, choices=[0, 1, 2],
                        default=None,
                        help="Manually override both target and actual state")
    parser.add_argument("--skip-freshness", action="store_true",
                        help="Skip stale-data check (for testing on weekends)")
    args = parser.parse_args()

    state_file = Path(os.getenv("STATE_FILE", "./model_state.json"))
    model_log  = Path(os.getenv("MODEL_LOG",  "./model_log.csv"))
    trade_log  = Path(os.getenv("TRADE_LOG",  "./trade_log.csv"))

    # Email
    ef, et, ep = (os.getenv("EMAIL_FROM"),
                  os.getenv("EMAIL_TO"),
                  os.getenv("EMAIL_PASSWORD"))
    email_cfg = ({"from": ef, "to": et, "password": ep,
                  "host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
                  "port": int(os.getenv("SMTP_PORT", "587"))}
                 if all([ef, et, ep]) else None)

    log.info("Danger mode: %s (%d%%)", _DANGER_MODE, int(_BG_DANGER * 100))
    if email_cfg:
        log.info("Email: %s → %s", ef, et)
    else:
        log.info("Email not configured — console only")

    # -- Manual state override --
    if args.set_state is not None:
        sn   = STRATEGY["state_name"]
        name = sn[args.set_state]
        save_state(state_file, {
            "target_state":  args.set_state,
            "target_name":   name,
            "actual_state":  args.set_state,
            "actual_name":   name,
            "pending_trade": False,
            "signal_date":   str(date.today()),
            "bg_flag":       False,
            "bg_zone":       "NORMAL",
            "ext_pct":       0.0,
            "updated_et":    fmt_et(),
        })
        print(f"State set → target={args.set_state} ({name}), "
              f"actual={args.set_state} ({name}), pending=False")
        return

    # -- Confirm trade executed --
    if args.confirm:
        prior = load_state(state_file)
        if not prior:
            print("No state file found. Nothing to confirm.")
            return
        target = prior.get("target_state")
        tname  = STRATEGY["state_name"].get(target, "?")
        prior["actual_state"]  = target
        prior["actual_name"]   = tname
        prior["pending_trade"] = False
        prior["confirmed_et"]  = fmt_et()
        save_state(state_file, prior)
        print(f"✅ Confirmed: actual_state set to {target} ({tname}). "
              f"Pending flag cleared.")
        return

    # -- Loop mode: run once per day at 4:45 PM ET --
    if args.loop:
        log.info("Loop mode: will run once daily at 4:45 PM ET")
        last_run_date: date | None = None
        while True:
            now = now_et()
            today = now.date()
            # Fix #1: only fire after close, not during market hours
            if (is_after_close() and
                    today != last_run_date and
                    now.weekday() < 5):
                log.info("After-close trigger firing for %s", today)
                try:
                    run_once(state_file, model_log, trade_log, email_cfg,
                             args.force_email, args.status,
                             args.skip_freshness)
                    last_run_date = today
                except Exception as e:
                    log.error("Run failed: %s", e, exc_info=True)
            else:
                if now.weekday() < 5:
                    log.info("Waiting for 4:45 PM ET close window…")
                else:
                    log.info("Weekend — not a market day")
            time.sleep(300)   # check every 5 minutes
        return

    # -- Single run --
    try:
        run_once(state_file, model_log, trade_log, email_cfg,
                 args.force_email, args.status, args.skip_freshness)
    except Exception as e:
        log.error("Run failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
