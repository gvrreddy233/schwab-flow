# schwab-flow

Capture options-chain snapshots from the [Charles Schwab API](https://developer.schwab.com/)
on a schedule, store them in SQLite, and write a per-run Excel workbook for review.
Built for tracking open-interest and volume changes over time (currently watching `PLTR`).

## What it does

- Pulls the full option chain (calls + puts) for each symbol in the watchlist.
- Appends every snapshot to `chains.db` (SQLite, the primary store).
- Writes one Excel workbook per run to `captures/`, with a **Summary** sheet,
  an **OI Changes** sheet (vs. the same time on a prior trading day), and a
  per-symbol chain sheet.
- Runs unattended on Windows Task Scheduler (pre-market, intraday, post-close).

### Notable behaviors

- **`-999` sentinels normalized to `NULL`.** Schwab returns `-999` for greeks/IV
  when its pricing model isn't running (e.g. market closed); these are stored as
  `NULL` rather than polluting the data.
- **Intrinsic value floored at zero.** Schwab reports raw `spot - strike` (negative
  for OTM); stored as `max(0, ...)`.
- **Holiday/weekend guard.** Scheduled runs query Schwab's market-hours endpoint and
  skip non-trading days. Pass `--force` to override for manual runs.
- **`chains.db` is tracked via [Git LFS](https://git-lfs.com/).** Install `git lfs`
  before cloning or the file comes down as a pointer instead of the real database.

## Repository layout

| Path | Purpose |
|------|---------|
| `chain_capture.py` | Main capture script (chain → SQLite + Excel). |
| `analyze.py`       | Query `chains.db` for daily summaries and IV/OI changes (CLI). |
| `auth_test.py`     | One-time OAuth setup + connectivity smoke test. |
| `schedule_tasks.ps1` | Registers the three Windows Scheduled Tasks. |
| `run_capture.bat`  | Wrapper invoked by Task Scheduler (sets UTF-8, runs the venv Python). |
| `chains.db`        | SQLite store of all snapshots (**Git LFS**). |
| `captures/`        | One Excel workbook per capture. |

## Prerequisites

- Windows + Python 3.12+
- A Schwab developer app (App Key, App Secret, callback URL)
- [Git LFS](https://git-lfs.com/) installed (`git lfs install`)

## Setup

```powershell
# 1. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt
```

Create a `.env` file in the project root (this file is gitignored — never commit it):

```ini
SCHWAB_APP_KEY=your_app_key
SCHWAB_APP_SECRET=your_app_secret
SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
```

Run the one-time auth flow. A browser opens for Schwab login; the resulting token is
saved to `schwab_token.json` (also gitignored) and reused on later runs:

```powershell
python auth_test.py
```

> Schwab refresh tokens expire periodically — when captures start failing auth,
> re-run `auth_test.py` to refresh the token.

## Usage

```powershell
# Capture the current chain for the watchlist
python chain_capture.py

# Force a capture even on a weekend/holiday (skips the trading-day guard)
python chain_capture.py --force
```

Analyze stored snapshots:

```powershell
python analyze.py list-captures
python analyze.py daily-summary
python analyze.py oi-change --symbol PLTR --top 30
python analyze.py iv-change --from 2026-05-18_1836 --to 2026-05-19_1643
```

The watchlist is set near the top of `chain_capture.py` (`WATCHLIST = ['PLTR']`).

## Scheduling

`schedule_tasks.ps1` registers three weekday tasks that run `run_capture.bat`:

| Task | Time (ET) |
|------|-----------|
| `SchwabFlow-PreMarket` | 09:00 |
| `SchwabFlow-Intraday`  | 12:30 |
| `SchwabFlow-PostClose` | 16:30 |

Register them from an **elevated** PowerShell prompt (the script is idempotent):

```powershell
powershell -ExecutionPolicy Bypass -File .\schedule_tasks.ps1
```

The trading-day guard means these tasks no-op on weekends and market holidays.

## Data model

`chains.db` has one table, `chain_snapshots`, keyed by
`(capture_ts, symbol, expiration, strike, option_type)`. Each row holds the quote
(bid/ask + sizes, last, mark), volume, open interest, greeks/IV, intrinsic/extrinsic
value, ITM flag, and expiration type for one contract at one capture time. New columns
are added in place via an `ALTER TABLE` migration in `init_db`, so an existing database
upgrades automatically.

## Security

`.env` and `schwab_token.json` hold credentials and are gitignored — keep them out of
version control. Rotate your Schwab app secret if it is ever exposed.
