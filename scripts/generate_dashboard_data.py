#!/usr/bin/env python3
"""
generate_dashboard_data.py

Builds docs/data/signals.json for the web dashboard.

This script does NOT reimplement either strategy. It imports download(),
run_v10() and run_v2() directly from the existing, untouched runner files
(tqqq_v10_daily_runner.py / tqqq_v2_daily_runner.py) and only adds the
bookkeeping needed to turn their output into structured JSON instead of
console text. The display-metric formulas (entry/exit gaps, bodyguard
clear distance, threshold tables, etc.) are copied verbatim from those
files' own print_dashboard() functions — same numbers, different output
format.

Rule: do NOT modify tqqq_v10_daily_runner.py, tqqq_v2_daily_runner.py, or
tqqq_live_model_v2.py. This is a new, separate file.

Usage:
    python scripts/generate_dashboard_data.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "data" / "signals.json"
HISTORY_BARS = 500          # trading days of chart history to ship (~2 years)
MAX_TRADES = 15             # most recent state changes to include per strategy


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v10_mod = load_module(ROOT / "tqqq_v10_daily_runner.py", "tqqq_v10_daily_runner")
v2_mod = load_module(ROOT / "tqqq_v2_daily_runner.py", "tqqq_v2_daily_runner")


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def days_in_state(state_arr) -> int:
    cur = state_arr[-1]
    count = 0
    for s in reversed(state_arr):
        if s == cur:
            count += 1
        else:
            break
    return count


def find_transitions(dates, state_arr, prices, max_n=MAX_TRADES):
    out = []
    for i in range(1, len(state_arr)):
        if state_arr[i] != state_arr[i - 1]:
            out.append({
                "date": str(pd.Timestamp(dates[i]).date()),
                "from": int(state_arr[i - 1]),
                "to": int(state_arr[i]),
                "spy": round(float(prices[i]), 2),
            })
    return out[-max_n:]


def trim(arr_like, n=HISTORY_BARS):
    return arr_like[-n:]


def fetch_sgov(index: pd.DatetimeIndex) -> np.ndarray:
    """
    SGOV (cash proxy) close, aligned to the same trading calendar as `index`.
    Used only to make the equity-curve visualization's "cash" leg realistic
    (SGOV drifts up slowly with T-bill yield) instead of assuming flat 0%.
    Falls back to flat 0% daily return if the fetch fails for any reason.
    """
    try:
        raw = yf.download("SGOV", start=v10_mod.BUFFER_START, auto_adjust=True, progress=False)
        s = raw["Close"]["SGOV"] if isinstance(raw.columns, pd.MultiIndex) else raw["Close"]
        s.index = pd.to_datetime(s.index).tz_localize(None)
        return s.reindex(index).ffill().values.astype(float)
    except Exception as e:
        print(f"Warning: SGOV fetch failed ({e}); equity curve will treat cash as flat.", file=sys.stderr)
        return np.full(len(index), np.nan)


def compute_equity(state_arr: np.ndarray, spy: np.ndarray, qqq: np.ndarray,
                    tqqq: np.ndarray, sgov: np.ndarray) -> np.ndarray:
    """
    Illustrative equity curve: $100 compounding through whatever the state
    machine held. Execution is close[i-1] -> close[i] for the position
    implied by state[i-1] (signal at yesterday's close, held through today),
    matching the "signal at close, execute next open" rule the runners
    document — approximated with close-to-close returns since only close
    prices are available. Not a full backtest; purely for the dashboard chart.
    """
    n = len(state_arr)
    spy_ret = pd.Series(spy).pct_change().values
    qqq_ret = pd.Series(qqq).pct_change().values
    tqqq_ret = pd.Series(tqqq).pct_change().values
    sgov_ret = pd.Series(sgov).pct_change().values
    ret_by_state = {2: tqqq_ret, 1: qqq_ret, 0: sgov_ret}

    equity = np.empty(n)
    equity[0] = 100.0
    for i in range(1, n):
        r = ret_by_state[int(state_arr[i - 1])][i]
        if np.isnan(r):
            r = 0.0
        equity[i] = equity[i - 1] * (1.0 + r)
    return equity


def load_prev(out_path: Path) -> dict:
    if not out_path.exists():
        return {}
    try:
        return json.loads(out_path.read_text())
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# v10  (formulas copied from tqqq_v10_daily_runner.print_dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def build_v10(close: pd.DataFrame, prev: dict, sgov: np.ndarray) -> dict:
    state_arr, bg_since_arr = v10_mod.run_v10(close)
    STATE_NAME = v10_mod.STATE_NAME
    STATE_TICKER = v10_mod.STATE_TICKER

    today = close.index[-1]
    cur = int(state_arr[-1])
    spy = float(close["SPY"].iloc[-1])
    qqq = float(close["QQQ"].iloc[-1])
    tqqq = float(close["TQQQ"].iloc[-1])

    sp = close["SPY"].values.astype(float)
    qq = close["QQQ"].values.astype(float)
    ss = pd.Series(sp)
    qs = pd.Series(qq)
    se180 = ss.rolling(180, min_periods=180).mean().values
    qe180 = qs.rolling(180, min_periods=180).mean().values
    se50 = ss.ewm(span=50, adjust=False).mean().values
    sb230 = ss.ewm(span=230, adjust=False).mean().values
    qb230 = qs.ewm(span=230, adjust=False).mean().values

    ext_spy = spy / sb230[-1] - 1.0
    ext_qqq = qqq / qb230[-1] - 1.0
    bg_active = bg_since_arr[-1] is not None
    bg_date = bg_since_arr[-1]
    n_days = days_in_state(state_arr)

    entry_ok_spy = spy > se180[-1] * 1.03
    entry_ok_qqq = qqq > qe180[-1] * 1.03
    entry_ready = entry_ok_spy and entry_ok_qqq

    prev_state = prev.get("state")
    changed = (prev_state is not None) and (prev_state != cur) and (prev.get("as_of_date") != str(today.date()))

    bodyguard = {
        "active": bool(bg_active),
        "since": str(bg_date.date()) if bg_active else None,
        "spy_ext_pct": round(ext_spy * 100, 2),
        "qqq_ext_pct": round(ext_qqq * 100, 2),
        "warn_pct": 10.0,
        "danger_pct": 30.0,
        "return_pct": 5.0,
    }
    if bg_active:
        spy_to_clear = (sb230[-1] * 1.05 - spy) / spy * 100
        qqq_to_clear = (qb230[-1] * 1.05 - qqq) / qqq * 100
        closer = "SPY" if abs(spy_to_clear) < abs(qqq_to_clear) else "QQQ"
        bodyguard["clear"] = {
            "spy_needed_pct": round(spy_to_clear, 2),
            "qqq_needed_pct": round(qqq_to_clear, 2),
            "closer": closer,
        }

    entry = {
        "description": "SPY & QQQ both > SMA180 × 1.03",
        "ready": bool(entry_ready),
        "spy_gap_pct": round((se180[-1] * 1.03 - spy) / spy * 100, 2),
        "qqq_gap_pct": round((qe180[-1] * 1.03 - qqq) / qqq * 100, 2),
    }

    exit_ = None
    if cur == 2:
        exit_trigger = se50[-1] * 0.94
        exit_ = {
            "description": "SPY < EMA50 × 0.94",
            "trigger_price": round(float(exit_trigger), 2),
            "spy_gap_pct": round((spy - exit_trigger) / spy * 100, 2),
        }

    equity_full = compute_equity(state_arr, sp, qq, close["TQQQ"].values.astype(float), sgov)
    equity_window = trim(equity_full)
    equity_window = (equity_window / equity_window[0] * 100.0)

    dates = [str(d.date()) for d in close.index]
    history = {
        "dates": trim(dates),
        "spy": [round(x, 2) for x in trim(sp.tolist())],
        "qqq": [round(x, 2) for x in trim(qq.tolist())],
        "tqqq": [round(x, 2) for x in trim(close["TQQQ"].values.astype(float).tolist())],
        "state": [int(x) for x in trim(state_arr.tolist())],
        "sma180_spy": [None if np.isnan(x) else round(float(x), 2) for x in trim(se180.tolist())],
        "sma180_qqq": [None if np.isnan(x) else round(float(x), 2) for x in trim(qe180.tolist())],
        "ema230_spy": [round(float(x), 2) for x in trim(sb230.tolist())],
        "ema230_qqq": [round(float(x), 2) for x in trim(qb230.tolist())],
        "equity": [round(float(x), 2) for x in equity_window.tolist()],
    }

    return {
        "id": "v10",
        "name": "v10 — SMA180 trend / EMA50 exit / EMA230 bodyguard",
        "as_of_date": str(today.date()),
        "state": cur,
        "state_name": STATE_NAME[cur],
        "state_ticker": STATE_TICKER[cur],
        "prev_state": prev_state,
        "prev_state_name": STATE_NAME.get(prev_state) if prev_state is not None else None,
        "changed": bool(changed),
        "days_in_state": n_days,
        "prices": {"spy": round(spy, 2), "qqq": round(qqq, 2), "tqqq": round(tqqq, 4)},
        "bodyguard": bodyguard,
        "entry": entry,
        "exit": exit_,
        "transitions": find_transitions(close.index, state_arr, sp),
        "history": history,
    }


# ─────────────────────────────────────────────────────────────────────────────
# v2  (formulas copied from tqqq_v2_daily_runner.print_dashboard)
# ─────────────────────────────────────────────────────────────────────────────

def build_v2(close: pd.DataFrame, prev: dict, sgov: np.ndarray) -> dict:
    state_arr, bg_since_arr, ext_arr = v2_mod.run_v2(close)
    STATE_NAME = v2_mod.STATE_NAME
    STATE_TICKER = v2_mod.STATE_TICKER

    today = close.index[-1]
    cur = int(state_arr[-1])

    spy = float(close["SPY"].iloc[-1])
    qqq = float(close["QQQ"].iloc[-1])
    tqqq = float(close["TQQQ"].iloc[-1])

    sp = close["SPY"].values.astype(float)
    qq = close["QQQ"].values.astype(float)
    ss = pd.Series(sp)
    qs = pd.Series(qq)

    sma160 = ss.rolling(160, min_periods=160).mean().values
    sma145 = ss.rolling(145, min_periods=145).mean().values
    ema210 = qs.ewm(span=210, adjust=False).mean().values

    ext = float(ext_arr[-1])
    bg_active = bg_since_arr[-1] is not None
    bg_since = bg_since_arr[-1]
    n_days_bg = v2_mod.trading_days_since(close.index, bg_since) if bg_active else 0
    n_days_cur = days_in_state(state_arr)

    entry_trigger = sma160[-1] * 1.04
    exit_trigger = sma145[-1] * 0.98
    bg_clear_price = ema210[-1] * 1.06
    entry_ready = spy > entry_trigger

    prev_state = prev.get("state")
    changed = (prev_state is not None) and (prev_state != cur) and (prev.get("as_of_date") != str(today.date()))

    if ext > 0.35:
        bg_zone = "DANGER"
    elif ext > 0.15:
        bg_zone = "WARN"
    elif ext > 0.06:
        bg_zone = "ELEVATED"
    else:
        bg_zone = "CLEAR"

    bodyguard = {
        "active": bool(bg_active),
        "since": str(bg_since.date()) if bg_active else None,
        "days_active": n_days_bg,
        "ext_pct": round(ext * 100, 2),
        "zone": bg_zone,
        "warn_pct": 15.0,
        "danger_pct": 35.0,
        "return_pct": 6.0,
    }
    if bg_active:
        pct_to_clear = (bg_clear_price - qqq) / qqq * 100
        bodyguard["clear"] = {
            "qqq_clear_price": round(float(bg_clear_price), 2),
            "qqq_needed_pct": round(pct_to_clear, 2),
        }

    entry = {
        "description": "SPY > SMA160 × 1.04",
        "ready": bool(entry_ready),
        "trigger_price": round(float(entry_trigger), 2),
        "spy_gap_pct": round((entry_trigger - spy) / spy * 100, 2),
    }

    exit_ = None
    if cur == 2:
        exit_ = {
            "description": "SPY < SMA145 × 0.98",
            "trigger_price": round(float(exit_trigger), 2),
            "spy_gap_pct": round((spy - exit_trigger) / spy * 100, 2),
        }

    thresholds = [
        {"label": "BG clear — QQQ ≤ EMA210+6%", "level": round(float(bg_clear_price), 2),
         "distance_pct": round((bg_clear_price - qqq) / qqq * 100, 2), "active": bool(bg_active)},
        {"label": "BG warn — QQQ > EMA210+15%", "level": round(float(ema210[-1] * 1.15), 2),
         "distance_pct": round((ema210[-1] * 1.15 - qqq) / qqq * 100, 2), "active": False},
        {"label": "BG danger — QQQ > EMA210+35%", "level": round(float(ema210[-1] * 1.35), 2),
         "distance_pct": round((ema210[-1] * 1.35 - qqq) / qqq * 100, 2), "active": False},
        {"label": "Entry — SPY > SMA160+4%", "level": round(float(entry_trigger), 2),
         "distance_pct": round((entry_trigger - spy) / spy * 100, 2), "active": bool(entry_ready)},
        {"label": "Exit — SPY < SMA145−2%", "level": round(float(exit_trigger), 2),
         "distance_pct": round((spy - exit_trigger) / spy * 100, 2), "active": bool(spy < exit_trigger)},
    ]

    equity_full = compute_equity(state_arr, sp, qq, close["TQQQ"].values.astype(float), sgov)
    equity_window = trim(equity_full)
    equity_window = (equity_window / equity_window[0] * 100.0)

    dates = [str(d.date()) for d in close.index]
    history = {
        "dates": trim(dates),
        "spy": [round(x, 2) for x in trim(sp.tolist())],
        "qqq": [round(x, 2) for x in trim(qq.tolist())],
        "tqqq": [round(x, 2) for x in trim(close["TQQQ"].values.astype(float).tolist())],
        "state": [int(x) for x in trim(state_arr.tolist())],
        "sma160_spy": [None if np.isnan(x) else round(float(x), 2) for x in trim(sma160.tolist())],
        "sma145_spy": [None if np.isnan(x) else round(float(x), 2) for x in trim(sma145.tolist())],
        "ema210_qqq": [round(float(x), 2) for x in trim(ema210.tolist())],
        "equity": [round(float(x), 2) for x in equity_window.tolist()],
    }

    return {
        "id": "v2",
        "name": "V2 — SMA160 entry / SMA145 exit / EMA210 bodyguard",
        "as_of_date": str(today.date()),
        "state": cur,
        "state_name": STATE_NAME[cur],
        "state_ticker": STATE_TICKER[cur],
        "prev_state": prev_state,
        "prev_state_name": STATE_NAME.get(prev_state) if prev_state is not None else None,
        "changed": bool(changed),
        "days_in_state": n_days_cur,
        "prices": {"spy": round(spy, 2), "qqq": round(qqq, 2), "tqqq": round(tqqq, 4)},
        "bodyguard": bodyguard,
        "entry": entry,
        "exit": exit_,
        "thresholds": thresholds,
        "transitions": find_transitions(close.index, state_arr, sp),
        "history": history,
    }


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("Fetching latest data...")
    close = v10_mod.download()
    if close[["SPY", "QQQ", "TQQQ"]].iloc[-1].isna().any():
        print("Warning: latest row has NaN close(s), retrying fetch once...", file=sys.stderr)
        close = v10_mod.download()
    if close[["SPY", "QQQ", "TQQQ"]].iloc[-1].isna().any():
        sys.exit("Latest close still has NaN after retry; aborting so stale-but-valid data is kept.")
    sgov = fetch_sgov(close.index)

    prev_all = load_prev(OUT_PATH)
    prev_v10 = (prev_all.get("strategies") or {}).get("v10", {})
    prev_v2 = (prev_all.get("strategies") or {}).get("v2", {})

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of_date": str(close.index[-1].date()),
        "strategies": {
            "v10": build_v10(close, prev_v10, sgov),
            "v2": build_v2(close, prev_v2, sgov),
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, allow_nan=False))
    print(f"Wrote {OUT_PATH}  ({OUT_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    sys.exit(main())
