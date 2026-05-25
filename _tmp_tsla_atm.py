"""ATM analysis for TSLA: what actually moved 5/20 16:30 -> 5/21 16:30 ET
in the captured strike window, plus gamma/skew snapshot for next session.
"""
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DB = Path(__file__).parent / 'chains.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

CUR = '2026-05-21T20:30:02+00:00'   # Thu PostClose
PREV = '2026-05-20T20:30:03+00:00'  # Wed PostClose

# 1) Top OI changes Wed->Thu PostClose
print("=== Top 15 TSLA OI changes (Wed 5/20 16:30 -> Thu 5/21 16:30 ET) ===")
sql_oi = """
SELECT c.expiration, c.days_to_expiration AS dte, c.strike, c.option_type AS t,
       c.open_interest AS oi_now, p.open_interest AS oi_prev,
       (c.open_interest - p.open_interest) AS d_oi,
       c.volume AS vol, c.iv AS iv, c.delta AS delta, c.mark AS mark
FROM chain_snapshots c
JOIN chain_snapshots p
  ON c.symbol=p.symbol AND c.expiration=p.expiration
 AND c.strike=p.strike AND c.option_type=p.option_type
WHERE c.symbol='TSLA' AND c.capture_ts=? AND p.capture_ts=?
  AND c.open_interest IS NOT NULL AND p.open_interest IS NOT NULL
ORDER BY ABS(c.open_interest - p.open_interest) DESC
LIMIT 15
"""
for r in conn.execute(sql_oi, (CUR, PREV)).fetchall():
    sign = '+' if r['d_oi'] > 0 else ''
    iv = f"{r['iv']:.1f}" if r['iv'] is not None else "  ?  "
    dlt = f"{r['delta']:+.2f}" if r['delta'] is not None else "  ?  "
    print(f"  {r['expiration']} DTE={r['dte']:>3}  ${r['strike']:>6.1f}{r['t']}  "
          f"ΔOI={sign}{int(r['d_oi']):>+7d}  OI={int(r['oi_now']):>7d}  "
          f"vol={int(r['vol'] or 0):>7d}  iv={iv}  Δ={dlt}  mark={r['mark']}")

# 2) Top volume Thu (regardless of OI change) - "what was actually traded"
print()
print("=== Top 15 TSLA contracts by Thu 5/21 volume ===")
sql_vol = """
SELECT expiration, days_to_expiration AS dte, strike, option_type AS t,
       volume AS vol, open_interest AS oi, iv, delta, mark
FROM chain_snapshots
WHERE symbol='TSLA' AND capture_ts=? AND volume IS NOT NULL
ORDER BY volume DESC
LIMIT 15
"""
for r in conn.execute(sql_vol, (CUR,)).fetchall():
    iv = f"{r['iv']:.1f}" if r['iv'] is not None else "  ?  "
    dlt = f"{r['delta']:+.2f}" if r['delta'] is not None else "  ?  "
    oi = int(r['oi']) if r['oi'] is not None else 0
    print(f"  {r['expiration']} DTE={r['dte']:>3}  ${r['strike']:>6.1f}{r['t']}  "
          f"vol={int(r['vol']):>7d}  OI={oi:>7d}  iv={iv}  Δ={dlt}  mark={r['mark']}")

# 3) Spot history (intraday Thu) to gauge price action
print()
print("=== TSLA spot at each capture (last 6 captures) ===")
for r in conn.execute(
    "SELECT capture_ts, MAX(underlying_price) AS spot FROM chain_snapshots "
    "WHERE symbol='TSLA' GROUP BY capture_ts ORDER BY capture_ts DESC LIMIT 6"
).fetchall():
    print(f"  {r['capture_ts']}  spot=${r['spot']:.2f}")

# 4) IV snapshot - ATM and skew for the 5/22 (0DTE), 5/29 weekly, 6/18 monthly
print()
print("=== ATM IV / skew snapshot (Thu 5/21 PostClose) ===")
spot = 417.85
for exp in ('2026-05-22', '2026-05-29', '2026-06-18'):
    print(f"\n  Expiration {exp}:")
    # ATM call/put closest to spot
    sql_skew = """
    SELECT strike, option_type AS t, iv, delta, open_interest AS oi, mark, volume AS vol
    FROM chain_snapshots
    WHERE symbol='TSLA' AND capture_ts=? AND expiration=?
      AND strike BETWEEN ? AND ?
    ORDER BY strike, option_type
    """
    lo = round(spot - 30)
    hi = round(spot + 30)
    rows = conn.execute(sql_skew, (CUR, exp, lo, hi)).fetchall()
    for r in rows:
        iv = f"{r['iv']:.1f}" if r['iv'] is not None else "  ?  "
        dlt = f"{r['delta']:+.2f}" if r['delta'] is not None else "  ?  "
        oi = int(r['oi']) if r['oi'] is not None else 0
        vol = int(r['vol']) if r['vol'] is not None else 0
        print(f"    ${r['strike']:>6.1f}{r['t']}  iv={iv}  Δ={dlt}  "
              f"OI={oi:>6d}  vol={vol:>6d}  mark={r['mark']}")

# 5) Gamma proximity: which strikes near spot have the largest OI?
#    These are the "magnet" levels for the next session via dealer hedging.
print()
print("=== Largest OI strikes within ±$25 of spot (Thu 5/21 PostClose) ===")
sql_magnet = """
SELECT expiration, days_to_expiration AS dte, strike, option_type AS t,
       open_interest AS oi, iv, delta
FROM chain_snapshots
WHERE symbol='TSLA' AND capture_ts=? AND strike BETWEEN ? AND ?
  AND open_interest IS NOT NULL AND open_interest > 0
ORDER BY open_interest DESC
LIMIT 15
"""
for r in conn.execute(sql_magnet, (CUR, spot - 25, spot + 25)).fetchall():
    iv = f"{r['iv']:.1f}" if r['iv'] is not None else "  ?  "
    dlt = f"{r['delta']:+.2f}" if r['delta'] is not None else "  ?  "
    print(f"  {r['expiration']} DTE={r['dte']:>3}  ${r['strike']:>6.1f}{r['t']}  "
          f"OI={int(r['oi']):>7d}  iv={iv}  Δ={dlt}")

conn.close()
