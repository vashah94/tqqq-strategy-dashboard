#!/usr/bin/env python3
"""
tqqq_v2_daily_runner.py

Run this every trading day AFTER market close (4 PM ET or later).

It tells you:
  - What the V2 signal is TODAY
  - Whether it changed from the previous trading day
  - Exactly what trade to make tomorrow morning (if any)
  - How far away each threshold is

Signal meanings:
  TQQQ  (state 2) — hold ProShares UltraPro QQQ  (ticker: TQQQ)
  QQQ   (state 1) — hold Invesco QQQ Trust        (ticker: QQQ)
  Cash  (state 0) — hold SGOV or money market      (ticker: SGOV)

Execution rule (same as backtest assumption):
  Signal generated at today's close  →  execute at tomorrow's open.

V2 parameters (from tqqq_live_model_v2.py — that file is READ ONLY):
  Entry : SPY > SMA160 × 1.04   (SPY-only)
  Exit  : SPY < SMA145 × 0.98   (SPY-only)
  BG    : QQQ EMA210 extension
            warn   > 15%  (if in TQQQ → step down to QQQ)
            danger > 35%  (force exit to Cash)
            return ≤  6%  (BG clears, normal signals resume)

NEW FILE — does not modify any existing file.
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
SIGNAL_FILE  = "tqqq_v2_last_signal.txt"

STATE_NAME   = {2: "TQQQ", 1: "QQQ", 0: "Cash/SGOV"}
STATE_TICKER = {2: "TQQQ", 1: "QQQ", 0: "SGOV"}


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
# V2 state machine  (exact copy from tqqq_live_model_v2.py — do not edit that file)
# ─────────────────────────────────────────────────────────────────────────────

def run_v2(close):
    EP, ET = 160, 0.04          # SPY SMA160 + 4% entry
    XP, XT = 145, -0.02         # SPY SMA145 - 2% exit
    BP, BW, BD, BR = 210, 0.15, 0.35, 0.06   # QQQ EMA210 bodyguard

    n   = len(close)
    sp  = close["SPY"].values.astype(float)
    qq  = close["QQQ"].values.astype(float)
    ss  = pd.Series(sp);  qs = pd.Series(qq)

    m160 = ss.rolling(EP, min_periods=EP).mean().values
    m145 = ss.rolling(XP, min_periods=XP).mean().values
    e210 = qs.ewm(span=BP, adjust=False).mean().values
    ext  = qq / e210 - 1.0

    vld  = ~(np.isnan(m160) | np.isnan(m145))
    if not vld.any():
        return np.zeros(n, dtype=np.int8), [None]*n, np.full(n, np.nan)

    fv   = int(np.argmax(vld))
    spv  = sp[fv:];  m1 = m160[fv:];  m2 = m145[fv:];  ev = ext[fv:]
    n2   = len(spv)
    st   = np.zeros(n2,  dtype=np.int8)
    bg_since_arr = [None] * n2
    cur  = np.int8(0);  bg = False;  bg_start = None

    for i in range(n2):
        prev_bg = bg
        e = ev[i]
        if e > BD:
            cur = np.int8(0);  bg = True
        elif e > BW:
            if cur == 2:  cur = np.int8(1)
            bg = True
        elif bg and e <= BR:
            bg = False

        if not bg:
            if cur < 2 and spv[i] > m1[i] * (1.0 + ET):
                cur = np.int8(2)
            elif cur == 2 and spv[i] < m2[i] * (1.0 + XT):
                cur = np.int8(0)
        st[i] = cur

        if bg and not prev_bg:
            bg_start = close.index[fv + i]
        if not bg:
            bg_start = None
        bg_since_arr[i] = bg_start

    fs        = np.zeros(n, dtype=np.int8)
    fs[fv:]   = st
    full_bg   = [None] * n
    full_bg[fv:] = bg_since_arr
    return fs, full_bg, ext


# ─────────────────────────────────────────────────────────────────────────────
# Signal persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_last_signal():
    if not os.path.exists(SIGNAL_FILE):
        return None, None
    try:
        lines = open(SIGNAL_FILE).read().strip().splitlines()
        return pd.Timestamp(lines[0].strip()), int(lines[1].strip())
    except Exception:
        return None, None


def save_signal(date, state):
    with open(SIGNAL_FILE, "w") as f:
        f.write(f"{date.date()}\n{state}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def days_in_state(state_arr):
    cur = state_arr[-1];  count = 0
    for s in reversed(state_arr):
        if s == cur:  count += 1
        else:         break
    return count


def trading_days_since(index, since_ts):
    """Count trading days between since_ts and last date in index."""
    if since_ts is None:
        return 0
    return int((index >= since_ts).sum())


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────────

def print_dashboard(close, state_arr, bg_since_arr, ext_arr, prev_date, prev_state):
    today = close.index[-1]
    cur   = int(state_arr[-1])

    spy   = float(close["SPY"].iloc[-1])
    qqq   = float(close["QQQ"].iloc[-1])
    tqqq  = float(close["TQQQ"].iloc[-1])

    # Re-compute indicators from raw series (needed for threshold distances)
    sp = close["SPY"].values.astype(float)
    qq = close["QQQ"].values.astype(float)
    ss = pd.Series(sp);  qs = pd.Series(qq)

    sma160 = float(ss.rolling(160, min_periods=160).mean().iloc[-1])
    sma145 = float(ss.rolling(145, min_periods=145).mean().iloc[-1])
    ema210 = float(qs.ewm(span=210, adjust=False).mean().iloc[-1])

    ext        = float(ext_arr[-1])           # QQQ extension vs EMA210
    bg_active  = bg_since_arr[-1] is not None
    bg_since   = bg_since_arr[-1]
    n_days_bg  = trading_days_since(close.index, bg_since) if bg_active else 0
    n_days_cur = days_in_state(state_arr)

    entry_trigger = sma160 * 1.04            # SPY must be ABOVE this to enter TQQQ
    exit_trigger  = sma145 * 0.98            # SPY must fall BELOW this to exit TQQQ
    bg_clear_price = ema210 * 1.06           # QQQ must drop TO OR BELOW this to clear BG

    entry_ready = spy > entry_trigger

    changed = (prev_state is not None) and (prev_state != cur)

    banner = "=" * 72
    print()
    print(banner)
    print(f"  V2 DAILY SIGNAL  —  {today.date()}")
    print(banner)
    print()

    # ── Signal box ────────────────────────────────────────────────────────────
    if changed:
        print("  !! SIGNAL CHANGED !!")
        print(f"  Was: {STATE_NAME[prev_state]:<10}  →  Now: {STATE_NAME[cur]}")
        print()
        print("  ACTION TOMORROW MORNING:")
        print("  ┌────────────────────────────────────────────┐")
        print(f"  │  SELL {STATE_TICKER[prev_state]:<6}  →  BUY {STATE_TICKER[cur]:<6}             │")
        print("  └────────────────────────────────────────────┘")
    else:
        print(f"  Signal: {STATE_NAME[cur]:<10}  (no change)")
        if prev_date:
            print(f"  Previous check: {prev_date.date()} — was {STATE_NAME[prev_state]}")
        print()
        print(f"  ACTION TOMORROW: HOLD {STATE_TICKER[cur]} — no trade needed.")

    print()
    print(f"  Current prices:  SPY ${spy:.2f}   QQQ ${qqq:.2f}   TQQQ ${tqqq:.4f}")
    print(f"  Days in current state: {n_days_cur}")

    # ── Bodyguard status ──────────────────────────────────────────────────────
    print()
    print("  BODYGUARD STATUS  (QQQ EMA210 extension)")
    print("  " + "─" * 68)

    ext_pct = ext * 100
    if   ext > 0.35:  bg_zone = "DANGER"
    elif ext > 0.15:  bg_zone = "WARN"
    elif ext > 0.06:  bg_zone = "ELEVATED (above return threshold)"
    else:             bg_zone = "CLEAR"

    print(f"  QQQ extension vs EMA210 : {ext_pct:+.2f}%   zone: {bg_zone}")
    print(f"    warn >15%   danger >35%   clears when ≤6%")
    print(f"  BG active : {'YES' if bg_active else 'NO'}"
          + (f"  (since {bg_since.date()}, {n_days_bg} trading days)" if bg_active else ""))

    if bg_active:
        pct_to_clear = (bg_clear_price - qqq) / qqq * 100   # negative = QQQ must drop
        print()
        print(f"  To clear BG: QQQ must drop to ≤ ${bg_clear_price:.2f}  "
              f"(≤6% above EMA210 ${ema210:.2f})")
        print(f"    QQQ today : ${qqq:.2f}")
        print(f"    QQQ needs : {pct_to_clear:+.1f}%  (${bg_clear_price:.2f})")

        if entry_ready:
            print()
            print("  Entry signal: READY  (SPY already above SMA160+4%)")
            print("  → If BG clears, V2 would enter TQQQ the SAME day.")
        else:
            spy_gap = (entry_trigger - spy) / spy * 100
            print()
            print(f"  Entry signal: not yet  (SPY needs {spy_gap:+.1f}% to reach SMA160+4%)")
            print(f"    SPY today        : ${spy:.2f}")
            print(f"    Entry trigger    : ${entry_trigger:.2f}  (SMA160 ${sma160:.2f} × 1.04)")

    else:
        # BG is clear — show entry or exit status
        print()
        if cur == 2:
            # In TQQQ — show exit trigger
            spy_vs_exit = (spy - exit_trigger) / spy * 100
            print("  Currently in TQQQ.  Exit fires if SPY < SMA145 × 0.98")
            print(f"    SPY today        : ${spy:.2f}")
            print(f"    Exit trigger     : ${exit_trigger:.2f}  (SMA145 ${sma145:.2f} × 0.98)")
            print(f"    SPY must fall    : {-spy_vs_exit:.1f}% from today to trigger exit")
        else:
            print("  BG is clear.  Watching entry signal.")
            spy_gap = (entry_trigger - spy) / spy * 100
            if entry_ready:
                print("  Entry signal: READY — V2 should already be in TQQQ (check state).")
            else:
                print(f"    SPY today        : ${spy:.2f}")
                print(f"    Entry trigger    : ${entry_trigger:.2f}  (SMA160 ${sma160:.2f} × 1.04)")
                print(f"    SPY needs        : {spy_gap:+.1f}% to trigger entry")

    # ── Threshold summary ─────────────────────────────────────────────────────
    print()
    print("  THRESHOLD DISTANCES")
    print("  " + "─" * 68)
    print(f"  {'Threshold':<35}  {'Level':>10}  {'Distance':>12}")
    print(f"  {'─'*35}  {'─'*10}  {'─'*12}")

    bg_dist = (bg_clear_price - qqq) / qqq * 100
    entry_dist = (entry_trigger - spy) / spy * 100
    exit_dist  = (exit_trigger  - spy) / spy * 100

    rows = [
        ("BG clear   QQQ ≤ EMA210+6%",    f"${bg_clear_price:.2f}",
         f"{bg_dist:+.1f}%  {'← ACTIVE' if bg_active else ''}"),
        ("BG warn    QQQ > EMA210+15%",    f"${ema210*1.15:.2f}",
         f"{(ema210*1.15-qqq)/qqq*100:+.1f}%"),
        ("BG danger  QQQ > EMA210+35%",    f"${ema210*1.35:.2f}",
         f"{(ema210*1.35-qqq)/qqq*100:+.1f}%"),
        ("Entry      SPY > SMA160+4%",     f"${entry_trigger:.2f}",
         f"{entry_dist:+.1f}%  {'← READY' if entry_ready else ''}"),
        ("Exit       SPY < SMA145−2%",     f"${exit_trigger:.2f}",
         f"{exit_dist:+.1f}%"),
    ]
    for label, level, dist in rows:
        print(f"  {label:<35}  {level:>10}  {dist:>12}")

    # ── Indicator values ──────────────────────────────────────────────────────
    print()
    print("  INDICATOR VALUES")
    print("  " + "─" * 68)
    print(f"  SPY  SMA160 : ${sma160:.2f}   (entry MA)")
    print(f"  SPY  SMA145 : ${sma145:.2f}   (exit MA)")
    print(f"  QQQ  EMA210 : ${ema210:.2f}   (BG reference)")
    print(f"  QQQ  ext    : {ext_pct:+.2f}%  (QQQ/EMA210 − 1)")

    print()
    print(banner)
    print(f"  HOLD: {STATE_NAME[cur]}  |  Ticker: {STATE_TICKER[cur]}  |  "
          f"Days in state: {n_days_cur}")
    print(banner)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("Fetching latest data...")
    close = download()

    state_arr, bg_since_arr, ext_arr = run_v2(close)
    cur_state = int(state_arr[-1])
    today     = close.index[-1]

    prev_date, prev_state = load_last_signal()

    print_dashboard(close, state_arr, bg_since_arr, ext_arr, prev_date, prev_state)

    save_signal(today, cur_state)


if __name__ == "__main__":
    main()
