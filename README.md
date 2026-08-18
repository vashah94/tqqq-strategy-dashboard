# TQQQ Strategy Dashboard

A read-only web dashboard for the two TQQQ rotation strategies in this repo
(**v10** and **V2**). It does not change either strategy — it only makes the
daily signal easier to read.

## How it fits together

```
tqqq_v10_daily_runner.py   ─┐
tqqq_v2_daily_runner.py    ─┤  untouched — same functions, same math
                            │
scripts/generate_dashboard_data.py   imports run_v10() / run_v2() from the
                            │        files above and writes their output as
                            ▼        JSON instead of console text
docs/data/signals.json
                            │
docs/index.html + assets/  reads that JSON and renders the dashboard
```

`generate_dashboard_data.py` never re-derives the entry/exit/bodyguard
*logic* — it calls the existing `run_v10()` / `run_v2()` state machines
directly and only reformats their numbers. The state-machine files
(`tqqq_v10_daily_runner.py`, `tqqq_v2_daily_runner.py`, `tqqq_live_model_v2.py`)
are never modified.

## Keeping it up to date

`.github/workflows/update-signals.yml` runs on GitHub Actions every weekday
at 21:30 UTC (after the US market close in both EST and EDT), regenerates
`docs/data/signals.json`, and commits it back to the repo. The page has no
backend — it just fetches that JSON file over plain HTTP, so no server or
API keys are needed.

## Publishing it

1. Push this repo to GitHub.
2. In **Settings → Pages**, set Source to "Deploy from a branch",
   branch `main`, folder `/docs`.
3. Your dashboard will be live at `https://<user>.github.io/<repo>/`.
4. The first page load needs `docs/data/signals.json` to exist — it's
   already committed with real data from the last local run. After that,
   the scheduled Action keeps it current. You can also trigger it manually
   from the **Actions** tab ("Update dashboard data" → **Run workflow**).

## Running it locally

```bash
python scripts/generate_dashboard_data.py   # refresh docs/data/signals.json
python -m http.server 8000 --directory docs # serve the dashboard
# open http://127.0.0.1:8000/
```

## What's on the page

- **Current signal** — what to hold today, and whether it changed from the
  last run (with the trade to make at tomorrow's open).
- **Entry / Exit** — how close price is to each strategy's trigger.
- **Bodyguard** — the risk overlay that de-risks after large run-ups.
- **All thresholds** — every level and its distance in one table.
- **Price history & position** — SPY/QQQ/TQQQ over the last ~2 years,
  shaded by which state the strategy was in.
- **Recent signal changes** — a log of the last position changes.
- **How this strategy works** — plain-English rules per strategy.

Not financial advice — this is a personal reference dashboard.
