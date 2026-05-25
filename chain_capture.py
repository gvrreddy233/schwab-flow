"""Capture option chain snapshots for a watchlist.

Writes each run to:
  - chains.db  : SQLite (append-only, primary store)
  - captures/  : one Excel workbook per capture, one sheet per symbol

One-shot: run captures the current chain for each symbol and exits.
Schedule externally (Task Scheduler) for periodic snapshots.
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Task Scheduler's stdout defaults to cp1252 on Windows; force UTF-8 so
# unicode chars (Δ, etc.) in log lines don't crash the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

import pandas as pd
from dotenv import load_dotenv
from schwab.auth import easy_client
from schwab.client import Client


WATCHLIST = ['PLTR']
STRIKE_COUNT = 30
PROJECT_DIR = Path(__file__).parent
DB_PATH = PROJECT_DIR / 'chains.db'
TOKEN_PATH = PROJECT_DIR / 'schwab_token.json'
CAPTURES_DIR = PROJECT_DIR / 'captures'
ET = ZoneInfo("America/New_York")

EXCEL_COLUMNS = [
    'expiration', 'days_to_expiration', 'expiration_type', 'strike', 'option_type',
    'bid', 'ask', 'bid_size', 'ask_size', 'mark', 'last',
    'volume', 'open_interest',
    'delta', 'gamma', 'theta', 'vega', 'rho', 'iv',
    'intrinsic_value', 'extrinsic_value', 'in_the_money',
    'underlying_price', 'capture_ts', 'symbol',
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_snapshots (
    capture_ts        TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    underlying_price  REAL,
    expiration        TEXT    NOT NULL,
    days_to_expiration INTEGER,
    strike            REAL    NOT NULL,
    option_type       TEXT    NOT NULL CHECK (option_type IN ('C', 'P')),
    expiration_type   TEXT,
    bid               REAL,
    ask               REAL,
    bid_size          INTEGER,
    ask_size          INTEGER,
    last              REAL,
    mark              REAL,
    volume            INTEGER,
    open_interest     INTEGER,
    delta             REAL,
    gamma             REAL,
    theta             REAL,
    vega              REAL,
    rho               REAL,
    iv                REAL,
    intrinsic_value   REAL,
    extrinsic_value   REAL,
    in_the_money      INTEGER,
    PRIMARY KEY (capture_ts, symbol, expiration, strike, option_type)
);
CREATE INDEX IF NOT EXISTS idx_symbol_ts ON chain_snapshots (symbol, capture_ts);
CREATE INDEX IF NOT EXISTS idx_symbol_exp_strike ON chain_snapshots (symbol, expiration, strike, option_type);
"""


# Columns added after the original schema shipped, with their SQL types.
# init_db ALTERs any that are missing so an existing chains.db migrates in place.
MIGRATION_COLUMNS = {
    'expiration_type': 'TEXT',
    'bid_size': 'INTEGER',
    'ask_size': 'INTEGER',
    'intrinsic_value': 'REAL',
    'extrinsic_value': 'REAL',
    'in_the_money': 'INTEGER',
}


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(chain_snapshots)")}
    for col, col_type in MIGRATION_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE chain_snapshots ADD COLUMN {col} {col_type}")
    conn.commit()


# Schwab returns -999 for greeks/IV when its theoretical-pricing model isn't
# running (e.g. market closed). Treat it as missing so it never pollutes the DB.
GREEK_SENTINEL = -999.0


def _clean_greek(value):
    if value is None:
        return None
    return None if value == GREEK_SENTINEL else value


def flatten_chain(chain: dict, capture_ts: str, symbol: str) -> list[dict]:
    rows = []
    underlying_price = chain.get('underlyingPrice')

    for side_key, opt_type in (('callExpDateMap', 'C'), ('putExpDateMap', 'P')):
        for exp_key, strikes in chain.get(side_key, {}).items():
            # exp_key looks like "2026-05-22:4" — expiration:DTE
            exp_date, _, dte_str = exp_key.partition(':')
            dte = int(dte_str) if dte_str.isdigit() else None

            for strike_str, contracts in strikes.items():
                if not contracts:
                    continue
                c = contracts[0]
                rows.append({
                    'capture_ts': capture_ts,
                    'symbol': symbol,
                    'underlying_price': underlying_price,
                    'expiration': exp_date,
                    'days_to_expiration': dte,
                    'strike': float(strike_str),
                    'option_type': opt_type,
                    'expiration_type': c.get('expirationType'),
                    'bid': c.get('bid'),
                    'ask': c.get('ask'),
                    'bid_size': c.get('bidSize'),
                    'ask_size': c.get('askSize'),
                    'last': c.get('last'),
                    'mark': c.get('mark'),
                    'volume': c.get('totalVolume'),
                    'open_interest': c.get('openInterest'),
                    'delta': _clean_greek(c.get('delta')),
                    'gamma': _clean_greek(c.get('gamma')),
                    'theta': _clean_greek(c.get('theta')),
                    'vega': _clean_greek(c.get('vega')),
                    'rho': _clean_greek(c.get('rho')),
                    'iv': _clean_greek(c.get('volatility')),
                    # Schwab reports raw (spot - strike), which goes negative for
                    # OTM options; floor at 0 to match the textbook definition.
                    'intrinsic_value': max(0.0, c['intrinsicValue']) if c.get('intrinsicValue') is not None else None,
                    'extrinsic_value': c.get('extrinsicValue'),
                    'in_the_money': int(c['inTheMoney']) if c.get('inTheMoney') is not None else None,
                })
    return rows


def is_trading_day(client) -> bool:
    """True if today is a US equity trading day (not a weekend or holiday).

    Uses Schwab's market-hours endpoint as the authoritative source: a trading
    day always returns a 'sessionHours' block (even before the 09:30 open, so the
    pre-market run isn't falsely skipped), while weekends/holidays omit it and
    report isOpen=false. Fails open (returns True) on any API error so a transient
    glitch never silently skips a real capture.
    """
    try:
        resp = client.get_market_hours([Client.MarketHours.Market.EQUITY])
        outer = resp.json().get('equity', {})
        # Inner key has historically been 'equity' or 'EQ'; don't hardcode it.
        eq = next(iter(outer.values()), {}) if outer else {}
        return bool(eq.get('sessionHours'))
    except Exception as e:
        print(f"WARNING: market-hours check failed ({e}); proceeding with capture.")
        return True


def capture_symbol(client, symbol: str, capture_ts: str):
    resp = client.get_option_chain(
        symbol,
        contract_type=Client.Options.ContractType.ALL,
        strike_count=STRIKE_COUNT,
        include_underlying_quote=True,
    )
    chain = resp.json()
    if chain.get('status') == 'FAILED':
        raise RuntimeError(f"chain fetch failed for {symbol}: {chain}")
    rows = flatten_chain(chain, capture_ts, symbol)
    return rows, chain.get('underlyingPrice')


SQLITE_COLUMNS = [
    'capture_ts', 'symbol', 'underlying_price', 'expiration', 'days_to_expiration',
    'strike', 'option_type', 'expiration_type', 'bid', 'ask', 'bid_size', 'ask_size',
    'last', 'mark', 'volume', 'open_interest',
    'delta', 'gamma', 'theta', 'vega', 'rho', 'iv',
    'intrinsic_value', 'extrinsic_value', 'in_the_money',
]

INSERT_SQL = (
    "INSERT OR REPLACE INTO chain_snapshots ("
    + ", ".join(SQLITE_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" * len(SQLITE_COLUMNS))
    + ")"
)

CHANGES_COLUMNS = [
    'symbol', 'expiration', 'dte', 'strike', 'type',
    'oi_delta', 'oi_pct', 'oi_now', 'oi_prev',
    'vol_delta', 'vol_now', 'vol_prev',
    'mark_now', 'mark_prev',
    'spot_now', 'spot_prev',
    'prev_capture_ts',
]


def rows_to_tuples(rows: list[dict]) -> list[tuple]:
    return [tuple(r[c] for c in SQLITE_COLUMNS) for r in rows]


SAME_TIME_TOLERANCE_MIN = 10


def find_same_time_prior_capture(conn: sqlite3.Connection, current_ts: str) -> str | None:
    """Most recent capture whose ET time-of-day is within ±10 min of current,
    on a strictly earlier ET calendar date. Returns None if no match."""
    current_et = datetime.fromisoformat(current_ts).astimezone(ET)
    current_minutes = current_et.hour * 60 + current_et.minute
    current_et_date = current_et.date()

    rows = conn.execute(
        "SELECT DISTINCT capture_ts FROM chain_snapshots "
        "WHERE capture_ts < ? ORDER BY capture_ts DESC",
        (current_ts,),
    ).fetchall()

    for (ts_str,) in rows:
        ts_et = datetime.fromisoformat(ts_str).astimezone(ET)
        if ts_et.date() == current_et_date:
            continue
        cand_minutes = ts_et.hour * 60 + ts_et.minute
        if abs(cand_minutes - current_minutes) <= SAME_TIME_TOLERANCE_MIN:
            return ts_str
    return None


def build_changes_df(conn: sqlite3.Connection, current_ts: str, previous_ts: str) -> pd.DataFrame:
    sql = """
    SELECT
        c.symbol,
        c.expiration,
        c.days_to_expiration AS dte,
        c.strike,
        c.option_type        AS type,
        c.open_interest      AS oi_now,
        p.open_interest      AS oi_prev,
        (c.open_interest - p.open_interest) AS oi_delta,
        c.volume             AS vol_now,
        p.volume             AS vol_prev,
        (c.volume - p.volume) AS vol_delta,
        c.mark               AS mark_now,
        p.mark               AS mark_prev,
        c.underlying_price   AS spot_now,
        p.underlying_price   AS spot_prev,
        p.capture_ts         AS prev_capture_ts
    FROM chain_snapshots c
    JOIN chain_snapshots p
      ON c.symbol      = p.symbol
     AND c.expiration  = p.expiration
     AND c.strike      = p.strike
     AND c.option_type = p.option_type
    WHERE c.capture_ts = ? AND p.capture_ts = ?
    """
    df = pd.read_sql_query(sql, conn, params=(current_ts, previous_ts))
    if df.empty:
        return df
    df = df.dropna(subset=['oi_delta'])
    df = df[df['oi_delta'] != 0].copy()
    if df.empty:
        return df
    df['oi_pct'] = (df['oi_delta'] / df['oi_prev'].replace(0, pd.NA)) * 100
    df['_abs'] = df['oi_delta'].abs()
    df = df.sort_values('_abs', ascending=False).drop(columns='_abs').reset_index(drop=True)
    return df.reindex(columns=CHANGES_COLUMNS)


SUMMARY_COLUMNS = [
    'symbol', 'spot', 'expirations', 'contracts',
    'call_oi', 'put_oi', 'pc_oi_ratio',
    'call_vol', 'put_vol', 'pc_vol_ratio',
    'max_call_oi_strike', 'max_call_oi',
    'max_put_oi_strike', 'max_put_oi',
    'max_call_vol_strike', 'max_call_vol',
    'max_put_vol_strike', 'max_put_vol',
    'top_voloi_contract', 'top_voloi_ratio',
    'atm_iv',
]


def build_summary_df(all_rows_by_symbol: dict[str, list[dict]]) -> pd.DataFrame:
    """One row per symbol of headline stats, computed ONLY from the captured
    chain numbers (no external fundamentals). Greeks/IV may be missing when the
    market is closed; those cells are left blank rather than guessed."""
    recs = []
    for symbol, rows in all_rows_by_symbol.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        for col in ('open_interest', 'volume', 'strike', 'iv', 'underlying_price'):
            df[col] = pd.to_numeric(df.get(col), errors='coerce')

        spot_series = df['underlying_price'].dropna()
        spot = float(spot_series.iloc[0]) if not spot_series.empty else None
        calls = df[df['option_type'] == 'C']
        puts = df[df['option_type'] == 'P']

        def total(frame, col):
            return int(frame[col].fillna(0).sum())

        def ratio(a, b):
            return round(a / b, 3) if b else None

        def arg_max(frame, col):
            f = frame.dropna(subset=[col])
            if f.empty or f[col].max() == 0:
                return None, None
            r = f.loc[f[col].idxmax()]
            return float(r['strike']), int(r[col])

        call_oi, put_oi = total(calls, 'open_interest'), total(puts, 'open_interest')
        call_vol, put_vol = total(calls, 'volume'), total(puts, 'volume')
        mco_strike, mco = arg_max(calls, 'open_interest')
        mpo_strike, mpo = arg_max(puts, 'open_interest')
        mcv_strike, mcv = arg_max(calls, 'volume')
        mpv_strike, mpv = arg_max(puts, 'volume')

        # Highest volume/OI = proxy for the day's most unusual activity.
        d = df.dropna(subset=['volume', 'open_interest'])
        d = d[d['open_interest'] > 0].copy()
        top_contract, top_ratio = None, None
        if not d.empty:
            d['voloi'] = d['volume'] / d['open_interest']
            r = d.loc[d['voloi'].idxmax()]
            top_contract = f"{r['strike']:.0f}{r['option_type']} {r['expiration']}"
            top_ratio = round(float(r['voloi']), 2)

        # ATM IV from the call nearest spot (blank if greeks not populated).
        atm_iv = None
        if spot is not None:
            cv = calls.dropna(subset=['iv'])
            if not cv.empty:
                r = cv.loc[(cv['strike'] - spot).abs().idxmin()]
                atm_iv = float(r['iv'])

        recs.append({
            'symbol': symbol, 'spot': spot,
            'expirations': df['expiration'].nunique(), 'contracts': len(df),
            'call_oi': call_oi, 'put_oi': put_oi, 'pc_oi_ratio': ratio(put_oi, call_oi),
            'call_vol': call_vol, 'put_vol': put_vol, 'pc_vol_ratio': ratio(put_vol, call_vol),
            'max_call_oi_strike': mco_strike, 'max_call_oi': mco,
            'max_put_oi_strike': mpo_strike, 'max_put_oi': mpo,
            'max_call_vol_strike': mcv_strike, 'max_call_vol': mcv,
            'max_put_vol_strike': mpv_strike, 'max_put_vol': mpv,
            'top_voloi_contract': top_contract, 'top_voloi_ratio': top_ratio,
            'atm_iv': atm_iv,
        })
    return pd.DataFrame(recs).reindex(columns=SUMMARY_COLUMNS)


def write_excel(
    all_rows_by_symbol: dict[str, list[dict]],
    changes_df: pd.DataFrame | None,
    excel_path: Path,
) -> None:
    CAPTURES_DIR.mkdir(exist_ok=True)
    summary_df = build_summary_df(all_rows_by_symbol)
    with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
        if not summary_df.empty:
            summary_df.to_excel(writer, sheet_name='Summary', index=False)
        if changes_df is not None and not changes_df.empty:
            changes_df.to_excel(writer, sheet_name='Changes', index=False)

        for symbol, rows in all_rows_by_symbol.items():
            if not rows:
                continue
            df = pd.DataFrame(rows)
            df = df.reindex(columns=EXCEL_COLUMNS)
            df = df.sort_values(['expiration', 'option_type', 'strike']).reset_index(drop=True)
            df.to_excel(writer, sheet_name=symbol, index=False)


def main():
    load_dotenv(Path(__file__).parent / '.env')

    client = easy_client(
        api_key=os.environ['SCHWAB_APP_KEY'],
        app_secret=os.environ['SCHWAB_APP_SECRET'],
        callback_url=os.environ['SCHWAB_CALLBACK_URL'],
        token_path=str(TOKEN_PATH),
        callback_timeout=600,
        interactive=False,
    )

    now_et = datetime.now(ET)

    # Holiday/weekend guard: scheduled runs skip non-trading days so we don't
    # store stale snapshots. Pass --force to override for manual/testing runs.
    force = '--force' in sys.argv
    if not force and not is_trading_day(client):
        print(f"{now_et:%Y-%m-%d} is not a US equity trading day "
              f"(weekend or market holiday). Skipping capture. Use --force to override.")
        return

    capture_ts = datetime.now(timezone.utc).isoformat(timespec='seconds')
    excel_path = CAPTURES_DIR / f"chain_{now_et:%Y-%m-%d_%H%M}ET.xlsx"

    print(f"Capture timestamp: {capture_ts}  ({now_et:%Y-%m-%d %H:%M %Z})")
    print(f"Database:          {DB_PATH}")
    print(f"Excel:             {excel_path}")
    print(f"Watchlist:         {', '.join(WATCHLIST)}")
    print()

    conn = sqlite3.connect(DB_PATH)
    rows_by_symbol: dict[str, list[dict]] = {}
    try:
        init_db(conn)

        total_rows = 0
        for symbol in WATCHLIST:
            try:
                rows, spot = capture_symbol(client, symbol, capture_ts)
            except Exception as e:
                print(f"  {symbol}: ERROR - {e}")
                continue

            rows_by_symbol[symbol] = rows
            conn.executemany(INSERT_SQL, rows_to_tuples(rows))
            conn.commit()
            spot_str = f"${spot:.2f}" if spot is not None else "?"
            print(f"  {symbol:6s} spot={spot_str:>10s}  rows={len(rows)}")
            total_rows += len(rows)

        changes_df = None
        prev_ts = find_same_time_prior_capture(conn, capture_ts)
        if prev_ts:
            changes_df = build_changes_df(conn, capture_ts, prev_ts)

        if rows_by_symbol:
            write_excel(rows_by_symbol, changes_df, excel_path)

        print()
        print(f"Inserted {total_rows} rows into {DB_PATH.name}.")
        print(f"Wrote Excel snapshot: {excel_path.name}")
        if prev_ts is None:
            print(f"Changes sheet: skipped (no prior capture near {now_et:%H:%M} ET on an earlier date).")
        else:
            prev_et = datetime.fromisoformat(prev_ts).astimezone(ET)
            gap_days = (now_et.date() - prev_et.date()).days
            label = f"vs {prev_et:%Y-%m-%d %H:%M} ET ({gap_days}d ago)"
            if changes_df is None or changes_df.empty:
                print(f"Changes sheet: skipped (no OI changes {label}).")
            else:
                top = changes_df.head(3)
                print(f"Changes sheet: {len(changes_df)} contracts moved {label}. Top 3:")
                for _, r in top.iterrows():
                    sign = '+' if r['oi_delta'] > 0 else ''
                    print(f"  {r['symbol']:5s} {r['expiration']} ${r['strike']:>7.1f} {r['type']}  ΔOI={sign}{int(r['oi_delta']):>+7d}")
    finally:
        conn.close()


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    main()
