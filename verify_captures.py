"""Verify a day's option-chain captures landed and greeks populated.

Usage:
  python verify_captures.py [YYYY-MM-DD]   # defaults to today (ET)

Checks, for the given ET date:
  1. Distinct capture timestamps + row counts in chains.db (expect ~3:
     PreMarket 09:00, Intraday 12:30, PostClose 16:30).
  2. Percent of that day's rows with a non-NULL delta (proves live greeks
     populated now the market is open, vs the -999->NULL closed-market path).
  3. The chain_<date>_*.xlsx files written to captures/.
  4. LastTaskResult of the three SchwabFlow-* Windows scheduled tasks (0 = ok).

Writes a plain-text report to captures/verify_<date>.txt and prints it.
"""
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Task Scheduler stdout defaults to cp1252; force UTF-8 so the report never crashes.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

ET = ZoneInfo("America/New_York")
PROJECT_DIR = Path(__file__).parent
DB_PATH = PROJECT_DIR / 'chains.db'
CAPTURES_DIR = PROJECT_DIR / 'captures'
TASKS = ['SchwabFlow-PreMarket', 'SchwabFlow-Intraday', 'SchwabFlow-PostClose']


def task_last_result(name: str) -> str:
    """Return the task's LastTaskResult via PowerShell, or an error string."""
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             f"(Get-ScheduledTask -TaskName '{name}' | Get-ScheduledTaskInfo).LastTaskResult"],
            capture_output=True, text=True, timeout=30,
        )
        val = out.stdout.strip()
        return val if val else f"(no result: {out.stderr.strip()})"
    except Exception as e:
        return f"(query failed: {e})"


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.now(ET).strftime('%Y-%m-%d')
    lines = []

    def emit(s=''):
        print(s)
        lines.append(s)

    emit("=" * 64)
    emit(f"Capture verification for ET date {date}")
    emit(f"Run at {datetime.now(ET):%Y-%m-%d %H:%M %Z}")
    emit("=" * 64)

    conn = sqlite3.connect(DB_PATH)
    # capture_ts is UTC ISO; daytime ET captures (13:00-20:30Z) fall on the same
    # UTC date, so a string-range filter on the date is sufficient here.
    lo, hi = date, date + 'T99'  # 'T99' sorts after any same-day timestamp
    caps = conn.execute(
        "SELECT capture_ts, COUNT(*) FROM chain_snapshots "
        "WHERE capture_ts >= ? AND capture_ts < ? GROUP BY capture_ts ORDER BY capture_ts",
        (lo, hi),
    ).fetchall()

    emit(f"\n[1] Captures in chains.db: {len(caps)} (expect ~3)")
    total = 0
    for ts, n in caps:
        ts_et = datetime.fromisoformat(ts).astimezone(ET).strftime('%H:%M %Z')
        emit(f"    {ts}  ({ts_et})  {n:,} rows")
        total += n
    emit(f"    total rows: {total:,}")

    emit("\n[2] Live greeks populated?")
    if total:
        nonnull = conn.execute(
            "SELECT COUNT(*) FROM chain_snapshots "
            "WHERE capture_ts >= ? AND capture_ts < ? AND delta IS NOT NULL",
            (lo, hi),
        ).fetchone()[0]
        pct = 100.0 * nonnull / total
        emit(f"    rows with non-NULL delta: {nonnull:,} / {total:,} ({pct:.1f}%)")
        emit("    -> looks like a live trading day" if pct > 50
             else "    -> greeks mostly NULL (market closed, or captured pre-open only)")
    else:
        emit("    no rows for this date.")
    conn.close()

    emit("\n[3] Excel files written:")
    files = sorted(CAPTURES_DIR.glob(f"chain_{date}_*.xlsx"))
    if files:
        for f in files:
            emit(f"    {f.name}  ({f.stat().st_size:,} bytes)")
    else:
        emit("    (none found)")

    emit("\n[4] Scheduled task results (0 = success):")
    for t in TASKS:
        emit(f"    {t:24s} LastTaskResult = {task_last_result(t)}")

    ok_caps = len(caps) >= 1
    emit("\n" + "=" * 64)
    emit("SUMMARY: " + ("captures ran and wrote rows."
                         if ok_caps else "NO captures found for this date — investigate."))
    emit("=" * 64)

    report = CAPTURES_DIR / f"verify_{date}.txt"
    report.write_text("\n".join(lines), encoding='utf-8')
    print(f"\nReport written to {report}")


if __name__ == "__main__":
    main()
