"""TSLA intraday positioning read.

Auto-detects the latest TSLA capture in chains.db and compares it to the
prior same-time-of-day capture (uses chain_capture.py's matching logic).

Reports:
  1. Current spot + capture timestamp
  2. Top OI changes vs prior same-time capture
  3. Top volume contracts in current session
  4. Gamma map: OI walls within +/-$25 of spot for the nearest expirations
  5. ATM IV snapshot (current expiry + next weekly + nearest monthly)

Run anytime after a fresh capture lands. No arguments needed.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ET = ZoneInfo("America/New_York")
DB = Path(__file__).parent / 'chains.db'
SYMBOL = 'TSLA'
SAME_TIME_TOLERANCE_MIN = 30  # widened from chain_capture.py's 10 min

# ---------------------------------------------------------------------------

def latest_capture(conn: sqlite3.Connection, symbol: str) -> str:
    row = conn.execute(
        "SELECT MAX(capture_ts) FROM chain_snapshots WHERE symbol=?",
        (symbol,),
    ).fetchone()
    return row[0]


def prior_same_time(conn: sqlite3.Connection, symbol: str, current_ts: str) -> str | None:
    """Most recent capture whose ET time-of-day is within +/-30 min of current,
    on a strictly earlier ET date.
    """
    cur_et = datetime.fromisoformat(current_ts).astimezone(ET)
    cur_minutes = cur_et.hour * 60 + cur_et.minute
    rows = conn.execute(
        "SELECT DISTINCT capture_ts FROM chain_snapshots "
        "WHERE symbol=? AND capture_ts < ? ORDER BY capture_ts DESC",
        (symbol, current_ts),
    ).fetchall()
    for (ts_str,) in rows:
        ts_et = datetime.fromisoformat(ts_str).astimezone(ET)
        if ts_et.date() == cur_et.date():
            continue
        cand_minutes = ts_et.hour * 60 + ts_et.minute
        if abs(cand_minutes - cur_minutes) <= SAME_TIME_TOLERANCE_MIN:
            return ts_str
    return None


def section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def fmt_row(row, cols: list[tuple[str, str, int]]) -> str:
    """cols = [(key, format, width)]"""
    out = []
    for key, fmt, width in cols:
        v = row[key] if key in row.keys() else None
        if v is None:
            s = '-'
        else:
            try:
                s = format(v, fmt)
            except (TypeError, ValueError):
                s = str(v)
        out.append(s.rjust(width) if width > 0 else s.ljust(-width))
    return '  '.join(out)


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    cur = latest_capture(conn, SYMBOL)
    if cur is None:
        print(f"No {SYMBOL} captures in {DB.name}.")
        return

    cur_et = datetime.fromisoformat(cur).astimezone(ET)
    spot_row = conn.execute(
        "SELECT MAX(underlying_price) AS spot FROM chain_snapshots "
        "WHERE symbol=? AND capture_ts=?",
        (SYMBOL, cur),
    ).fetchone()
    spot = spot_row['spot']

    prev = prior_same_time(conn, SYMBOL, cur)

    section("Header")
    print(f"  Symbol:           {SYMBOL}")
    print(f"  Latest capture:   {cur_et:%Y-%m-%d %H:%M %Z}  ({cur})")
    print(f"  Spot:             ${spot:.2f}")
    if prev:
        prev_et = datetime.fromisoformat(prev).astimezone(ET)
        print(f"  Comparing to:     {prev_et:%Y-%m-%d %H:%M %Z}  ({prev})")
        prev_spot = conn.execute(
            "SELECT MAX(underlying_price) AS s FROM chain_snapshots WHERE symbol=? AND capture_ts=?",
            (SYMBOL, prev),
        ).fetchone()['s']
        if prev_spot:
            change = spot - prev_spot
            print(f"  Spot vs prior:    ${prev_spot:.2f} -> ${spot:.2f}  ({change:+.2f}, {change/prev_spot*100:+.2f}%)")
    else:
        print(f"  Comparing to:     (no prior same-time capture found)")

    # --- 1. Spot trajectory across today's captures ---
    section("Spot trajectory (today's captures, ET)")
    today_str = cur_et.strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT capture_ts, MAX(underlying_price) AS spot FROM chain_snapshots "
        "WHERE symbol=? AND substr(capture_ts,1,10) IN (?, ?) "
        "GROUP BY capture_ts ORDER BY capture_ts",
        (SYMBOL, today_str, cur_et.date().isoformat()),
    ).fetchall()
    for r in rows:
        ts_et = datetime.fromisoformat(r['capture_ts']).astimezone(ET)
        print(f"  {ts_et:%Y-%m-%d %H:%M %Z}   spot=${r['spot']:.2f}")

    # --- 2. Top OI changes vs prior same-time ---
    if prev:
        section(f"Top 15 OI changes vs prior same-time capture")
        sql = """
        SELECT c.expiration, c.days_to_expiration AS dte, c.strike, c.option_type AS t,
               c.open_interest AS oi_now, p.open_interest AS oi_prev,
               (c.open_interest - p.open_interest) AS d_oi,
               c.volume AS vol, c.iv, c.delta, c.mark
        FROM chain_snapshots c
        JOIN chain_snapshots p USING (symbol, expiration, strike, option_type)
        WHERE c.symbol=? AND c.capture_ts=? AND p.capture_ts=?
          AND c.open_interest IS NOT NULL AND p.open_interest IS NOT NULL
        ORDER BY ABS(c.open_interest - p.open_interest) DESC
        LIMIT 15
        """
        for r in conn.execute(sql, (SYMBOL, cur, prev)).fetchall():
            iv = f"{r['iv']:>5.1f}" if r['iv'] is not None else "  -  "
            dlt = f"{r['delta']:+.2f}" if r['delta'] is not None else "  -  "
            sign = '+' if r['d_oi'] > 0 else ''
            print(
                f"  {r['expiration']} DTE={r['dte']:>3}  "
                f"${r['strike']:>6.1f}{r['t']}  "
                f"dOI={sign}{int(r['d_oi']):>+7d}  "
                f"OI={int(r['oi_now']):>7d}  "
                f"vol={int(r['vol'] or 0):>7d}  "
                f"iv={iv}  d={dlt}  mark={r['mark']}"
            )

    # --- 3. Top volume in current capture ---
    section("Top 15 contracts by volume (current capture)")
    sql_vol = """
    SELECT expiration, days_to_expiration AS dte, strike, option_type AS t,
           volume AS vol, open_interest AS oi, iv, delta, mark
    FROM chain_snapshots
    WHERE symbol=? AND capture_ts=? AND volume IS NOT NULL
    ORDER BY volume DESC
    LIMIT 15
    """
    for r in conn.execute(sql_vol, (SYMBOL, cur)).fetchall():
        iv = f"{r['iv']:>5.1f}" if r['iv'] is not None else "  -  "
        dlt = f"{r['delta']:+.2f}" if r['delta'] is not None else "  -  "
        oi = int(r['oi']) if r['oi'] is not None else 0
        print(
            f"  {r['expiration']} DTE={r['dte']:>3}  "
            f"${r['strike']:>6.1f}{r['t']}  "
            f"vol={int(r['vol']):>7d}  "
            f"OI={oi:>7d}  "
            f"iv={iv}  d={dlt}  mark={r['mark']}"
        )

    # --- 4. Gamma map: OI walls near spot ---
    section(f"OI walls within +/-$25 of spot (${spot:.2f})")
    sql_walls = """
    SELECT expiration, days_to_expiration AS dte, strike, option_type AS t,
           open_interest AS oi, iv, delta
    FROM chain_snapshots
    WHERE symbol=? AND capture_ts=? AND strike BETWEEN ? AND ?
      AND open_interest IS NOT NULL AND open_interest > 0
    ORDER BY open_interest DESC
    LIMIT 15
    """
    for r in conn.execute(sql_walls, (SYMBOL, cur, spot - 25, spot + 25)).fetchall():
        iv = f"{r['iv']:>5.1f}" if r['iv'] is not None else "  -  "
        dlt = f"{r['delta']:+.2f}" if r['delta'] is not None else "  -  "
        print(
            f"  {r['expiration']} DTE={r['dte']:>3}  "
            f"${r['strike']:>6.1f}{r['t']}  "
            f"OI={int(r['oi']):>7d}  "
            f"iv={iv}  d={dlt}"
        )

    # --- 5. ATM IV snapshot for nearest 3 expirations ---
    section("ATM IV snapshot (nearest 3 expirations)")
    exps = conn.execute(
        "SELECT DISTINCT expiration, days_to_expiration FROM chain_snapshots "
        "WHERE symbol=? AND capture_ts=? AND days_to_expiration >= 0 "
        "ORDER BY days_to_expiration LIMIT 3",
        (SYMBOL, cur),
    ).fetchall()
    for e in exps:
        print(f"\n  Expiration {e['expiration']} (DTE={e['days_to_expiration']}):")
        # Get the strikes closest to ATM
        atm = conn.execute(
            "SELECT strike, option_type AS t, iv, delta, open_interest AS oi, volume AS vol, mark "
            "FROM chain_snapshots WHERE symbol=? AND capture_ts=? AND expiration=? "
            "AND strike BETWEEN ? AND ? ORDER BY strike, option_type",
            (SYMBOL, cur, e['expiration'], spot - 10, spot + 10),
        ).fetchall()
        for r in atm:
            iv = f"{r['iv']:>5.1f}" if r['iv'] is not None else "  -  "
            dlt = f"{r['delta']:+.2f}" if r['delta'] is not None else "  -  "
            oi = int(r['oi']) if r['oi'] is not None else 0
            vol = int(r['vol']) if r['vol'] is not None else 0
            print(
                f"    ${r['strike']:>6.1f}{r['t']}  "
                f"iv={iv}  d={dlt}  "
                f"OI={oi:>6d}  vol={vol:>6d}  mark={r['mark']}"
            )

    conn.close()


if __name__ == "__main__":
    main()
