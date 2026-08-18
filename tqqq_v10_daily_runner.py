#!/usr/bin/env python3
"""
tqqq_v10_daily_runner.py

Run this every trading day AFTER market close (4 PM ET or later).

It tells you:
  - What the v10 signal is TODAY
  - Whether it changed from the previous trading day
  - Exactly what trade to make tomorrow morning (if any)
  - How far away the next signal change is

Signal meanings:
  TQQQ  (state 2) — hold ProShares UltraPro QQQ  (ticker: TQQQ)
  QQQ   (state 1) — hold Invesco QQQ Trust        (ticker: QQQ)
  Cash  (state 0) — hold SGOV or money market      (ticker: SGOV)

Execution rule (same as backtest assumption):
  Signal generated at today's close  →  execute at tomorrow's open.

Rule: do NOT modify any existing file.
"""
from __future__ import annotations
import sys, warnings, os
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit("pip install yfinance")

BUFFER_START = "2008-09-01"
SIGNAL_FILE  = "tqqq_v10_last_signal.txt"   # stores yesterday's signal for change detection

STATE_NAME   = {2: "TQQQ", 1: "QQQ", 0: "Cash/SGOV"}
STATE_TICKER = {2: "TQQQ", 1: "QQQ", 0: "SGOV"}
STATE_COLOR  = {2: "TQQQ", 1: "QQQ", 0: "Cash"}


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────

def download():
    raw = yf.download(["SPY", "QQQ", "TQQQ"], start=BUFFER_START,
                      auto_adjust=True, progress=False)
    c = raw["Close"].copy()
    c.index = pd.to_datetime(c.index).tz_localize(None)
    return c.dropna(how="all").ffill()


# ─────────────────────────────────────────────────────────────────────────────
# v10 state machine  (exact copy from tqqq_live_signal.py — no edits)
# ─────────────────────────────────────────────────────────────────────────────

def run_v10(close):
    EP,EB,XP,XB,BP,BW,BD,BR = 180,0.03,50,0.06,230,0.10,0.30,0.05
    spy = close["SPY"].values.astype(np.float64)
    qqq = close["QQQ"].values.astype(np.float64)
    n   = len(spy)
    ss  = pd.Series(spy); qs = pd.Series(qqq)

    se  = ss.rolling(EP, min_periods=EP).mean().values
    qe  = qs.rolling(EP, min_periods=EP).mean().values
    sx  = ss.ewm(span=XP, adjust=False).mean().values
    sb  = ss.ewm(span=BP, adjust=False).mean().values
    qb  = qs.ewm(span=BP, adjust=False).mean().values

    en  = (spy > se * (1 + EB)) & (qqq > qe * (1 + EB))
    ex  = spy < sx * (1 - XB)
    es  = spy / sb - 1.0
    eq_ = qqq / qb - 1.0
    bw  = (es > BW) | (eq_ > BW)
    bd  = (es > BD) | (eq_ > BD)
    br  = (es <= BR) | (eq_ <= BR)
    for a in (en, ex, bw, bd, br):
        np.nan_to_num(a.astype(np.float64), copy=False, nan=0.0)

    st  = np.zeros(n, dtype=np.int8)
    cur = 0; bg = False
    bg_since = [None] * n

    for i in range(1, n):
        prev_bg = bg
        if bool(bd[i]):
            cur = 0; bg = True
        elif bool(bw[i]):
            if cur == 2: cur = 1
            bg = True
        elif bg and bool(br[i]):
            bg = False
        if not bg:
            if cur < 2:
                if bool(en[i]): cur = 2
            else:
                if bool(ex[i]): cur = 0
        st[i] = cur
        bg_since[i] = bg_since[i-1]
        if bg and not prev_bg:
            bg_since[i] = close.index[i]
        if not bg:
            bg_since[i] = None

    return st, bg_since


# ─────────────────────────────────────────────────────────────────────────────
# Signal persistence (detect changes day-to-day)
# ─────────────────────────────────────────────────────────────────────────────

def load_last_signal():
    if not os.path.exists(SIGNAL_FILE):
        return None, None
    try:
        with open(SIGNAL_FILE) as f:
            lines = f.read().strip().splitlines()
        date_str = lines[0].strip()
        state    = int(lines[1].strip())
        return pd.Timestamp(date_str), state
    except Exception:
        return None, None


def save_signal(date, state):
    with open(SIGNAL_FILE, "w") as f:
        f.write("%s\n%d\n" % (date.date(), state))


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard print
# ─────────────────────────────────────────────────────────────────────────────

def days_in_state(state_arr):
    cur = state_arr[-1]
    count = 0
    for s in reversed(state_arr):
        if s == cur: count += 1
        else: break
    return count


def print_dashboard(close, state_arr, bg_since_arr, prev_date, prev_state):
    today   = close.index[-1]
    cur     = int(state_arr[-1])
    spy     = close["SPY"].iloc[-1]
    qqq     = close["QQQ"].iloc[-1]
    tqqq    = close["TQQQ"].iloc[-1]

    # compute indicators
    sp = close["SPY"].values.astype(float)
    qq = close["QQQ"].values.astype(float)
    ss = pd.Series(sp); qs = pd.Series(qq)
    se180  = ss.rolling(180, min_periods=180).mean().values[-1]
    qe180  = qs.rolling(180, min_periods=180).mean().values[-1]
    se50   = ss.ewm(span=50,  adjust=False).mean().values[-1]
    sb230  = ss.ewm(span=230, adjust=False).mean().values[-1]
    qb230  = qs.ewm(span=230, adjust=False).mean().values[-1]

    ext_spy = spy / sb230 - 1.0
    ext_qqq = qqq / qb230 - 1.0
    bg_active = bg_since_arr[-1] is not None
    bg_date   = bg_since_arr[-1]
    n_days    = days_in_state(state_arr)

    entry_ok_spy = spy > se180 * 1.03
    entry_ok_qqq = qqq > qe180 * 1.03
    entry_ready  = entry_ok_spy and entry_ok_qqq

    # ── change detection ─────────────────────────────────────────────────────
    changed = (prev_state is not None) and (prev_state != cur)

    banner = "=" * 72
    print()
    print(banner)
    print("  v10 DAILY SIGNAL  —  %s" % today.date())
    print(banner)
    print()

    # big signal box
    if changed:
        print("  !! SIGNAL CHANGED !!")
        print("  Was: %-10s  →  Now: %s" % (
            STATE_NAME[prev_state], STATE_NAME[cur]))
        print()
        print("  ACTION TOMORROW MORNING:")
        print("  ┌────────────────────────────────────────────┐")
        print("  │  SELL %-6s  →  BUY %-6s             │" % (
            STATE_TICKER[prev_state], STATE_TICKER[cur]))
        print("  └────────────────────────────────────────────┘")
    else:
        print("  Signal: %-10s  (no change)" % STATE_NAME[cur])
        if prev_date:
            print("  Previous check: %s — was %s" % (prev_date.date(), STATE_NAME[prev_state]))
        print()
        print("  ACTION TOMORROW: HOLD %s — no trade needed." % STATE_TICKER[cur])

    print()
    print("  Current prices:  SPY $%.2f   QQQ $%.2f   TQQQ $%.4f" % (
        spy, qqq, tqqq))
    print()

    # ── BG status ────────────────────────────────────────────────────────────
    print("  BODYGUARD STATUS")
    print("  " + "─" * 68)
    print("  SPY ext vs EMA230 : %+.2f%%   (warn >10%%, danger >30%%)" % (ext_spy * 100))
    print("  QQQ ext vs EMA230 : %+.2f%%   (warn >10%%, danger >30%%)" % (ext_qqq * 100))
    print("  BG active         : %s%s" % (
        "YES" if bg_active else "NO",
        ("  (since %s, %d trading days)" % (bg_date.date(), n_days))
        if bg_active else ""))

    if bg_active:
        # how far to clear (EITHER SPY or QQQ ≤5% above EMA230)
        spy_to_clear = (sb230 * 1.05 - spy) / spy * 100   # negative = how much spy must drop
        qqq_to_clear = (qb230 * 1.05 - qqq) / qqq * 100
        closer = "SPY" if abs(spy_to_clear) < abs(qqq_to_clear) else "QQQ"
        closer_pct = min(abs(spy_to_clear), abs(qqq_to_clear))
        print()
        print("  To clear BG (EITHER SPY or QQQ must reach ≤5%% above EMA230):")
        print("    SPY needs: %+.1f%%  (to $%.2f)" % (spy_to_clear, sb230 * 1.05))
        print("    QQQ needs: %+.1f%%  (to $%.2f)" % (qqq_to_clear, qb230 * 1.05))
        print("    → %s is closer  (%+.1f%% from today)" % (closer, -closer_pct))
        if entry_ready:
            print()
            print("  Entry signal: READY  (both SPY and QQQ already above SMA180+3%)")
            print("  → If BG clears, v10 would enter TQQQ the SAME day.")
        else:
            print()
            print("  Entry signal: not yet met")
            spy_gap = (se180 * 1.03 - spy) / spy * 100
            qqq_gap = (qe180 * 1.03 - qqq) / qqq * 100
            if not entry_ok_spy: print("    SPY still needs %+.1f%% to meet entry" % spy_gap)
            if not entry_ok_qqq: print("    QQQ still needs %+.1f%% to meet entry" % qqq_gap)
    else:
        print()
        if cur == 2:
            # in TQQQ — show exit trigger
            exit_trigger = se50 * 0.94
            spy_vs_exit  = (spy - exit_trigger) / spy * 100
            print("  Currently in TQQQ.  Exit triggers if SPY < EMA50 × 0.94")
            print("  Exit trigger: $%.2f   SPY must fall %.1f%% from today to exit" % (
                exit_trigger, spy_vs_exit))
        else:
            print("  BG is clear.  Watching entry signal.")
            spy_gap = (se180 * 1.03 - spy) / spy * 100
            qqq_gap = (qe180 * 1.03 - qqq) / qqq * 100
            print("  SPY vs entry: %+.1f%%   QQQ vs entry: %+.1f%%" % (spy_gap, qqq_gap))

    print()
    print(banner)
    print("  HOLD: %s  |  Ticker to own: %s  |  Days in state: %d" % (
        STATE_NAME[cur], STATE_TICKER[cur], n_days))
    print(banner)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("Fetching latest data...")
    close = download()

    state_arr, bg_since_arr = run_v10(close)
    cur_state = int(state_arr[-1])
    today     = close.index[-1]

    prev_date, prev_state = load_last_signal()

    print_dashboard(close, state_arr, bg_since_arr, prev_date, prev_state)

    save_signal(today, cur_state)


if __name__ == "__main__":
    main()
