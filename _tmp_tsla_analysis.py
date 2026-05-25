"""One-shot analysis: check OI/IV deltas for TSLA strikes of interest
in chains.db from the 2026-05-21 flow report.

Strikes flagged in the flow tape:
  $180 C (5/22/26 and 6/18/26)
  $220 C (5/22/26 and 5/29/26)
  $700 P (8/21/26)
"""
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DB = Path(__file__).parent / 'chains.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 1. What capture timestamps exist for TSLA?
print("=== TSLA captures present ===")
ts_rows = conn.execute(
    "SELECT DISTINCT capture_ts FROM chain_snapshots WHERE symbol='TSLA' "
    "ORDER BY capture_ts DESC LIMIT 12"
).fetchall()
for r in ts_rows:
    print(f"  {r['capture_ts']}")

# 2. Spot + strike range for TSLA's 5/21 16:30 ET capture
print()
print("=== TSLA strike range in latest capture ===")
latest = conn.execute(
    "SELECT capture_ts, underlying_price, MIN(strike) AS lo, MAX(strike) AS hi, "
    "       COUNT(DISTINCT strike) AS n_strikes, COUNT(DISTINCT expiration) AS n_exps "
    "FROM chain_snapshots WHERE symbol='TSLA' "
    "AND capture_ts = (SELECT MAX(capture_ts) FROM chain_snapshots WHERE symbol='TSLA')"
).fetchone()
print(f"  capture_ts:  {latest['capture_ts']}")
print(f"  spot:        ${latest['underlying_price']:.2f}")
print(f"  strike low:  {latest['lo']}")
print(f"  strike high: {latest['hi']}")
print(f"  n_strikes:   {latest['n_strikes']}")
print(f"  n_expirations: {latest['n_exps']}")

# 3. Specific contracts of interest — do they exist in DB?
print()
print("=== Strikes of interest (in latest capture, by expiration) ===")
targets = [
    ('2026-05-22', 180.0, 'C'),
    ('2026-06-18', 180.0, 'C'),
    ('2026-05-22', 220.0, 'C'),
    ('2026-05-29', 220.0, 'C'),
    ('2026-08-21', 700.0, 'P'),
]
latest_ts = latest['capture_ts']
for exp, strike, opt_type in targets:
    row = conn.execute(
        "SELECT bid, ask, mark, volume, open_interest, iv, delta "
        "FROM chain_snapshots WHERE symbol='TSLA' AND capture_ts=? "
        "AND expiration=? AND strike=? AND option_type=?",
        (latest_ts, exp, strike, opt_type)
    ).fetchone()
    label = f"TSLA {exp} ${strike:g}{opt_type}"
    if row is None:
        print(f"  {label:30s} NOT CAPTURED (outside strike window)")
    else:
        print(f"  {label:30s} mark={row['mark']:>7} vol={row['volume']:>7} "
              f"OI={row['open_interest']:>7} iv={row['iv']} delta={row['delta']}")

# 4. List the expirations captured for TSLA in latest snapshot
print()
print("=== TSLA expirations captured (latest snapshot) ===")
exps = conn.execute(
    "SELECT expiration, days_to_expiration, COUNT(*) AS n "
    "FROM chain_snapshots WHERE symbol='TSLA' AND capture_ts=? "
    "GROUP BY expiration ORDER BY expiration",
    (latest_ts,)
).fetchall()
for r in exps:
    print(f"  {r['expiration']}  DTE={r['days_to_expiration']:>4}  rows={r['n']}")

conn.close()
