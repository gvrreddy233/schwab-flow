"""Query chains.db for daily summaries and intra-/inter-day changes.

Usage:
  python analyze.py list-captures
  python analyze.py daily-summary
  python analyze.py iv-change [--from TS] [--to TS] [--symbol SYM] [--min-dte N] [--top N] [--type C|P]
  python analyze.py oi-change [--from TS] [--to TS] [--symbol SYM] [--min-dte N] [--top N] [--type C|P]
  python analyze.py report    [--days N] [--top N] [--min-dte N]

Timestamp formats accepted for --from / --to:
  latest                       most recent capture (default for --to)
  prev                         most recent capture from a strictly earlier ET date (default for --from)
  YYYY-MM-DD_HHMM              ET wall-clock, matched within +/- 10 min (e.g. 2026-05-19_1643)
  YYYY-MM-DDTHH:MM:SS+00:00    exact ISO capture_ts as stored in DB

Examples:
  python analyze.py iv-change                              # latest vs prev day
  python analyze.py iv-change --symbol TSLA --top 30
  python analyze.py oi-change --from 2026-05-18_1836 --to 2026-05-19_1643
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


DB_PATH = Path(__file__).parent / 'chains.db'
CAPTURES_DIR = Path(__file__).parent / 'captures'
ET = ZoneInfo('America/New_York')
SAME_TIME_TOLERANCE_MIN = 10

# Bucket boundaries (ET minutes since midnight).
# Pre-market < 09:30; Intraday 09:30-16:00; PostClose >= 16:00.
BUCKETS = [
    ('PreMarket', 0,         9 * 60 + 30),
    ('Intraday',  9 * 60 + 30, 16 * 60),
    ('PostClose', 16 * 60,    24 * 60),
]
BUCKET_CENTER_MIN = {'PreMarket': 9 * 60, 'Intraday': 12 * 60 + 30, 'PostClose': 16 * 60 + 30}


def list_capture_ts(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        'SELECT DISTINCT capture_ts FROM chain_snapshots ORDER BY capture_ts'
    ).fetchall()]


def resolve_ts(conn: sqlite3.Connection, spec: str, anchor_ts: str | None = None) -> str:
    """Resolve a user-provided timestamp spec to a stored capture_ts.

    anchor_ts is only used for the 'prev' keyword (look for prior-date capture
    near anchor's ET time-of-day).
    """
    all_ts = list_capture_ts(conn)
    if not all_ts:
        raise SystemExit('chains.db has no captures')

    if spec == 'latest':
        return all_ts[-1]

    if spec == 'prev':
        if anchor_ts is None:
            anchor_ts = all_ts[-1]
        anchor_et = datetime.fromisoformat(anchor_ts).astimezone(ET)
        anchor_min = anchor_et.hour * 60 + anchor_et.minute
        # Prefer same-time-of-day match within tolerance on a strictly earlier date.
        for ts in reversed(all_ts):
            if ts >= anchor_ts:
                continue
            ts_et = datetime.fromisoformat(ts).astimezone(ET)
            if ts_et.date() == anchor_et.date():
                continue
            cand_min = ts_et.hour * 60 + ts_et.minute
            if abs(cand_min - anchor_min) <= SAME_TIME_TOLERANCE_MIN:
                return ts
        # Fallback: any capture from a strictly earlier ET date (most recent).
        for ts in reversed(all_ts):
            ts_et = datetime.fromisoformat(ts).astimezone(ET)
            if ts_et.date() < anchor_et.date():
                return ts
        raise SystemExit('no prior-date capture found in chains.db')

    # ET shorthand: YYYY-MM-DD_HHMM
    if len(spec) == 15 and spec[10] == '_':
        try:
            target = datetime.strptime(spec, '%Y-%m-%d_%H%M').replace(tzinfo=ET)
        except ValueError:
            pass
        else:
            target_min = target.hour * 60 + target.minute
            best, best_gap = None, None
            for ts in all_ts:
                ts_et = datetime.fromisoformat(ts).astimezone(ET)
                if ts_et.date() != target.date():
                    continue
                gap = abs((ts_et.hour * 60 + ts_et.minute) - target_min)
                if gap <= SAME_TIME_TOLERANCE_MIN and (best_gap is None or gap < best_gap):
                    best, best_gap = ts, gap
            if best:
                return best
            raise SystemExit(
                f'no capture within +/-{SAME_TIME_TOLERANCE_MIN}min of {spec} ET '
                f'(use list-captures to see what exists)'
            )

    # Exact ISO timestamp
    if spec in all_ts:
        return spec

    raise SystemExit(f'could not resolve timestamp: {spec!r}')


def fmt_ts(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ET).strftime('%Y-%m-%d %H:%M ET')


def cmd_list_captures(conn: sqlite3.Connection, args) -> None:
    rows = conn.execute('''
        SELECT capture_ts, COUNT(*) AS rows,
               COUNT(DISTINCT symbol) AS symbols,
               SUM(open_interest) AS sum_oi,
               SUM(volume) AS sum_vol
        FROM chain_snapshots
        GROUP BY capture_ts
        ORDER BY capture_ts
    ''').fetchall()
    print(f"{'ET capture time':<22} {'rows':>6} {'syms':>5} {'sum_OI':>14} {'sum_Vol':>14}")
    for ts, n, syms, oi, vol in rows:
        print(f'{fmt_ts(ts):<22} {n:>6} {syms:>5} {oi or 0:>14,} {vol or 0:>14,}')


def cmd_daily_summary(conn: sqlite3.Connection, args) -> None:
    df = pd.read_sql_query('''
        SELECT capture_ts, symbol,
               COUNT(*) AS rows,
               SUM(open_interest) AS sum_oi,
               SUM(volume) AS sum_vol,
               MAX(underlying_price) AS spot
        FROM chain_snapshots
        GROUP BY capture_ts, symbol
        ORDER BY capture_ts, symbol
    ''', conn)
    df['capture_et'] = df['capture_ts'].map(fmt_ts)
    out = df[['capture_et', 'symbol', 'spot', 'rows', 'sum_oi', 'sum_vol']]
    print(out.to_string(index=False))


def _diff_df(conn, curr_ts, prev_ts, symbol, min_dte, opt_type):
    sql = '''
    SELECT c.symbol, c.expiration, c.days_to_expiration AS dte, c.strike,
           c.option_type AS type,
           p.iv AS iv_prev, c.iv AS iv_now, (c.iv - p.iv) AS iv_delta,
           p.open_interest AS oi_prev, c.open_interest AS oi_now,
           (c.open_interest - p.open_interest) AS oi_delta,
           p.volume AS vol_prev, c.volume AS vol_now,
           (c.volume - p.volume) AS vol_delta,
           p.mark AS mark_prev, c.mark AS mark_now,
           c.underlying_price AS spot_now
    FROM chain_snapshots c
    JOIN chain_snapshots p
      ON c.symbol=p.symbol AND c.expiration=p.expiration
     AND c.strike=p.strike AND c.option_type=p.option_type
    WHERE c.capture_ts=? AND p.capture_ts=?
    '''
    params = [curr_ts, prev_ts]
    if symbol:
        sql += ' AND c.symbol=?'
        params.append(symbol.upper())
    if min_dte is not None:
        sql += ' AND c.days_to_expiration >= ?'
        params.append(min_dte)
    if opt_type:
        sql += ' AND c.option_type=?'
        params.append(opt_type.upper())
    return pd.read_sql_query(sql, conn, params=params)


def cmd_iv_change(conn: sqlite3.Connection, args) -> None:
    curr = resolve_ts(conn, args.to_ts)
    prev = resolve_ts(conn, args.from_ts, anchor_ts=curr)
    min_dte = 1 if args.min_dte is None else args.min_dte  # filter 0-DTE artifacts by default
    df = _diff_df(conn, curr, prev, args.symbol, min_dte, args.type)
    df = df.dropna(subset=['iv_prev', 'iv_now'])
    df = df[(df['iv_prev'] > 0) & (df['iv_now'] > 0)].copy()
    if df.empty:
        print('no rows after filters')
        return
    df['abs_iv_delta'] = df['iv_delta'].abs()
    df = df.sort_values('abs_iv_delta', ascending=False).head(args.top)
    print(f'IV change: {fmt_ts(prev)}  ->  {fmt_ts(curr)}  (min_dte={min_dte})')
    print(df[['symbol', 'expiration', 'dte', 'strike', 'type',
              'iv_prev', 'iv_now', 'iv_delta',
              'oi_prev', 'oi_now', 'oi_delta',
              'vol_prev', 'vol_now', 'vol_delta',
              'mark_now']].to_string(index=False))


def cmd_oi_change(conn: sqlite3.Connection, args) -> None:
    curr = resolve_ts(conn, args.to_ts)
    prev = resolve_ts(conn, args.from_ts, anchor_ts=curr)
    min_dte = 0 if args.min_dte is None else args.min_dte
    df = _diff_df(conn, curr, prev, args.symbol, min_dte, args.type)
    df = df.dropna(subset=['oi_prev', 'oi_now'])
    df = df[df['oi_delta'] != 0].copy()
    if df.empty:
        print('no OI changes')
        return
    df['oi_pct'] = (df['oi_delta'] / df['oi_prev'].replace(0, pd.NA)) * 100
    df['abs_oi_delta'] = df['oi_delta'].abs()
    df = df.sort_values('abs_oi_delta', ascending=False).head(args.top)
    print(f'OI change: {fmt_ts(prev)}  ->  {fmt_ts(curr)}  (min_dte={min_dte})')
    print(df[['symbol', 'expiration', 'dte', 'strike', 'type',
              'oi_prev', 'oi_now', 'oi_delta', 'oi_pct',
              'vol_now', 'mark_now']].round(2).to_string(index=False))


def bucket_captures(conn: sqlite3.Connection, days_back: int) -> pd.DataFrame:
    """Return one capture_ts per (date, bucket) for the most-recent `days_back`
    ET dates. Picks the capture closest to each bucket's center time."""
    rows = conn.execute(
        'SELECT DISTINCT capture_ts FROM chain_snapshots ORDER BY capture_ts'
    ).fetchall()
    df = pd.DataFrame(rows, columns=['capture_ts'])
    if df.empty:
        return df
    et = df['capture_ts'].map(lambda t: datetime.fromisoformat(t).astimezone(ET))
    df['date'] = et.map(lambda d: d.date())
    df['min_of_day'] = et.map(lambda d: d.hour * 60 + d.minute)
    df['bucket'] = df['min_of_day'].map(
        lambda m: next((b for b, lo, hi in BUCKETS if lo <= m < hi), 'PostClose')
    )
    df['bucket_gap'] = df.apply(
        lambda r: abs(r['min_of_day'] - BUCKET_CENTER_MIN[r['bucket']]), axis=1
    )
    # pick the capture nearest each bucket's center
    df = df.sort_values(['date', 'bucket', 'bucket_gap'])
    picked = df.groupby(['date', 'bucket'], as_index=False).first()
    # keep only the most-recent N dates
    recent_dates = sorted(picked['date'].unique())[-days_back:]
    picked = picked[picked['date'].isin(recent_dates)].copy()
    picked['label'] = picked['date'].astype(str) + ' ' + picked['bucket']
    return picked.sort_values(['bucket', 'date']).reset_index(drop=True)


def _pivot_for_bucket(conn, picks: pd.DataFrame, bucket: str, metric: str,
                       top: int, min_dte: int) -> pd.DataFrame:
    """Pivot a metric (oi or iv) for one bucket across all available dates."""
    sub = picks[picks['bucket'] == bucket].sort_values('date')
    if sub.empty:
        return pd.DataFrame()
    ts_list = sub['capture_ts'].tolist()
    placeholders = ','.join('?' * len(ts_list))
    sql = f'''
        SELECT capture_ts, symbol, expiration, days_to_expiration AS dte,
               strike, option_type AS type, open_interest AS oi, iv, volume AS vol
        FROM chain_snapshots
        WHERE capture_ts IN ({placeholders}) AND days_to_expiration >= ?
    '''
    df = pd.read_sql_query(sql, conn, params=[*ts_list, min_dte])
    if df.empty:
        return df
    ts_to_label = dict(zip(sub['capture_ts'], sub['date'].astype(str)))
    df['day'] = df['capture_ts'].map(ts_to_label)
    # dte decreases day-over-day; keep latest dte as metadata, don't pivot on it
    latest_day = max(ts_to_label.values())
    dte_latest = (df[df['day'] == latest_day]
                  .set_index(['symbol', 'expiration', 'strike', 'type'])['dte']
                  .to_dict())
    pivot = df.pivot_table(
        index=['symbol', 'expiration', 'strike', 'type'],
        columns='day',
        values=metric,
        aggfunc='last',
    ).reset_index()
    pivot['dte'] = pivot.apply(
        lambda r: dte_latest.get((r['symbol'], r['expiration'], r['strike'], r['type'])),
        axis=1,
    )
    day_cols = [c for c in pivot.columns if c not in
                ('symbol', 'expiration', 'dte', 'strike', 'type')]
    if len(day_cols) < 2:
        return pivot.reindex(columns=['symbol', 'expiration', 'dte', 'strike', 'type', *day_cols])
    day_cols_sorted = sorted(day_cols)
    first, last = day_cols_sorted[0], day_cols_sorted[-1]
    pivot['change'] = pivot[last] - pivot[first]
    pivot['abs_change'] = pivot['change'].abs()
    pivot = pivot.dropna(subset=[first, last])
    pivot = pivot.sort_values('abs_change', ascending=False).head(top)
    return pivot.drop(columns='abs_change').reindex(
        columns=['symbol', 'expiration', 'dte', 'strike', 'type',
                 *day_cols_sorted, 'change']
    )


VOL_CONFIRM_THRESHOLD = 100   # vol_now needed for OI delta to count as real flow
PHANTOM_IV_THRESHOLD = 5      # |iv_delta| above which low-vol moves are flagged as stale-mark


def _classify_direction(row) -> str:
    if pd.isna(row['oi_delta']) or row['oi_delta'] == 0:
        return ''
    typ = row['type']
    if row['oi_delta'] > 0:
        return 'Calls added (bullish/covered)' if typ == 'C' else 'Puts added (bearish/hedge)'
    return 'Calls closed' if typ == 'C' else 'Puts closed'


def _build_summary(conn: sqlite3.Connection, curr_ts: str, prev_ts: str) -> dict:
    sql = '''
    SELECT c.symbol, c.expiration, c.days_to_expiration AS dte, c.strike,
           c.option_type AS type,
           p.iv AS iv_prev, c.iv AS iv_now,
           p.open_interest AS oi_prev, c.open_interest AS oi_now,
           c.volume AS vol_now, c.mark AS mark_now, c.underlying_price AS spot_now
    FROM chain_snapshots c
    JOIN chain_snapshots p
      ON c.symbol=p.symbol AND c.expiration=p.expiration
     AND c.strike=p.strike AND c.option_type=p.option_type
    WHERE c.capture_ts=? AND p.capture_ts=?
    '''
    df = pd.read_sql_query(sql, conn, params=(curr_ts, prev_ts))
    df['oi_delta'] = df['oi_now'] - df['oi_prev']
    df['iv_delta'] = df['iv_now'] - df['iv_prev']

    # 1. Headline movers: volume-confirmed top OI changes
    real = df[(df['vol_now'].fillna(0) >= VOL_CONFIRM_THRESHOLD) &
              (df['oi_delta'].fillna(0) != 0)].copy()
    real['abs_oi'] = real['oi_delta'].abs()
    headline = (real.sort_values('abs_oi', ascending=False).head(25)
                .drop(columns='abs_oi'))
    headline['direction'] = headline.apply(_classify_direction, axis=1)
    headline = headline[['symbol', 'expiration', 'dte', 'strike', 'type',
                         'oi_prev', 'oi_now', 'oi_delta', 'vol_now',
                         'mark_now', 'spot_now', 'direction']]

    # 2. Per-symbol positioning (volume-confirmed only)
    def _agg(g):
        return pd.Series({
            'calls_added': g.loc[(g['type'] == 'C') & (g['oi_delta'] > 0), 'oi_delta'].sum(),
            'calls_closed': g.loc[(g['type'] == 'C') & (g['oi_delta'] < 0), 'oi_delta'].sum(),
            'puts_added': g.loc[(g['type'] == 'P') & (g['oi_delta'] > 0), 'oi_delta'].sum(),
            'puts_closed': g.loc[(g['type'] == 'P') & (g['oi_delta'] < 0), 'oi_delta'].sum(),
        })
    pos = real.groupby('symbol').apply(_agg, include_groups=False).reset_index()
    if not pos.empty:
        pos['net_call_oi'] = pos['calls_added'] + pos['calls_closed']
        pos['net_put_oi'] = pos['puts_added'] + pos['puts_closed']
        def _signal(r):
            nc, np_ = r['net_call_oi'], r['net_put_oi']
            if abs(nc) < 500 and abs(np_) < 500:
                return 'quiet'
            if nc > 0 and np_ > 0 and min(nc, np_) > 0.4 * max(nc, np_):
                return 'vol buying (both sides)'
            if nc - np_ > 1000:
                return 'bullish skew (calls > puts)'
            if np_ - nc > 1000:
                return 'bearish skew (puts > calls)'
            return 'mixed'
        pos['signal'] = pos.apply(_signal, axis=1)

    # 3. IV regime: front-month vs long-term mean IV change per symbol
    iv = df[(df['iv_now'] > 0) & (df['iv_prev'] > 0)].copy()
    front = iv[(iv['dte'] >= 1) & (iv['dte'] <= 30)]
    longt = iv[iv['dte'] >= 180]
    iv_front = front.groupby('symbol')['iv_delta'].agg(['mean', 'median', 'count']).round(2)
    iv_front.columns = ['front_iv_mean', 'front_iv_median', 'front_n']
    iv_long = longt.groupby('symbol')['iv_delta'].agg(['mean', 'median', 'count']).round(2)
    iv_long.columns = ['long_iv_mean', 'long_iv_median', 'long_n']
    iv_regime = iv_front.join(iv_long, how='outer').reset_index()

    def _iv_signal(r):
        f, l = r.get('front_iv_mean'), r.get('long_iv_mean')
        if pd.isna(f) and pd.isna(l):
            return ''
        f = 0 if pd.isna(f) else f
        l = 0 if pd.isna(l) else l
        if f > 2 and l > 2:
            return 'vol bid across the curve'
        if f > 2:
            return 'front-month vol bid'
        if l > 2:
            return 'long-term vol bid'
        if f < -2 and l < -2:
            return 'vol crush across the curve'
        if abs(f) < 1 and abs(l) < 1:
            return 'flat'
        return 'mixed'
    iv_regime['iv_signal'] = iv_regime.apply(_iv_signal, axis=1)

    # 4. Data quality: phantom IV moves (big IV move, no volume)
    phantom = df[(df['iv_delta'].abs() >= PHANTOM_IV_THRESHOLD) &
                 (df['vol_now'].fillna(0) < 5) &
                 (df['iv_now'] > 0) & (df['iv_prev'] > 0)]
    quality = (phantom.groupby('symbol').size()
               .reset_index(name='phantom_iv_rows'))
    if not quality.empty:
        quality['note'] = 'IV moves on near-zero-volume strikes — likely stale marks; discount LEAP IV signal'

    return {
        'headline': headline,
        'positioning': pos,
        'iv_regime': iv_regime,
        'quality': quality,
        'curr_ts': curr_ts,
        'prev_ts': prev_ts,
    }


def _write_summary_sheet(writer, summary: dict) -> None:
    sheet = 'Summary'
    header = pd.DataFrame({
        'A': [
            f"Comparison: {fmt_ts(summary['prev_ts'])}  ->  {fmt_ts(summary['curr_ts'])}",
            f"Volume threshold for 'real' flow: vol_now >= {VOL_CONFIRM_THRESHOLD}",
            f"Phantom IV flag: |iv_delta| >= {PHANTOM_IV_THRESHOLD} with vol_now < 5",
        ]
    })
    header.to_excel(writer, sheet_name=sheet, index=False, header=False, startrow=0)

    row = 4
    sections = [
        ('Headline OI Movers (volume-confirmed)', summary['headline']),
        ('Per-Symbol Net Positioning (real flow only)', summary['positioning']),
        ('IV Regime (mean IV change by tenor bucket)', summary['iv_regime']),
        ('Data Quality Flags', summary['quality']),
    ]
    for title, df in sections:
        title_df = pd.DataFrame({'A': [title]})
        title_df.to_excel(writer, sheet_name=sheet, index=False, header=False, startrow=row)
        row += 1
        if df is None or df.empty:
            pd.DataFrame({'A': ['(no rows)']}).to_excel(
                writer, sheet_name=sheet, index=False, header=False, startrow=row)
            row += 2
            continue
        df.to_excel(writer, sheet_name=sheet, index=False, startrow=row)
        row += len(df) + 3


def cmd_report(conn: sqlite3.Connection, args) -> None:
    CAPTURES_DIR.mkdir(exist_ok=True)
    picks = bucket_captures(conn, args.days)
    if picks.empty:
        raise SystemExit('chains.db has no captures')

    now_et = datetime.now(ET)
    out_path = CAPTURES_DIR / f'report_{now_et:%Y-%m-%d_%H%M}ET.xlsx'

    captures_summary = picks[['date', 'bucket', 'label', 'capture_ts']].copy()
    captures_summary['capture_et'] = captures_summary['capture_ts'].map(fmt_ts)
    captures_summary = captures_summary[['date', 'bucket', 'capture_et']]

    # Pick comparison anchors for the Summary sheet: latest vs prev-day same-bucket.
    all_ts = list_capture_ts(conn)
    curr_ts = all_ts[-1]
    prev_ts = None
    try:
        prev_ts = resolve_ts(conn, 'prev', anchor_ts=curr_ts)
    except SystemExit:
        prev_ts = None

    with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
        if prev_ts:
            summary_data = _build_summary(conn, curr_ts, prev_ts)
            _write_summary_sheet(writer, summary_data)
        captures_summary.to_excel(writer, sheet_name='Captures', index=False)

        for bucket, *_ in BUCKETS:
            for metric, label in (('oi', 'OI'), ('iv', 'IV'), ('vol', 'Vol')):
                df = _pivot_for_bucket(conn, picks, bucket, metric,
                                        args.top, args.min_dte)
                if df.empty:
                    continue
                sheet = f'{bucket}_{label}'[:31]
                df.to_excel(writer, sheet_name=sheet, index=False)

    print(f'Wrote {out_path}')
    print()
    print('Captures included:')
    print(captures_summary.to_string(index=False))

    if getattr(args, 'auto_open', False):
        import os
        os.startfile(str(out_path))  # Windows: opens with default .xlsx handler


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='Query chains.db captures.')
    sub = p.add_subparsers(dest='cmd', required=True)

    sub.add_parser('list-captures', help='list every capture with row/OI/vol totals')
    sub.add_parser('daily-summary', help='per-capture, per-symbol totals')

    rp = sub.add_parser('report',
                        help='multi-day Excel report bucketed by PreMarket/Intraday/PostClose')
    rp.add_argument('--days', type=int, default=6,
                    help='lookback window in ET dates (default 6)')
    rp.add_argument('--top', type=int, default=30,
                    help='top-N movers per sheet (default 30)')
    rp.add_argument('--min-dte', type=int, default=1,
                    help='filter contracts with DTE >= N (default 1)')
    rp.add_argument('--auto-open', action='store_true',
                    help='open the generated Excel file after writing')

    for name in ('iv-change', 'oi-change'):
        sp = sub.add_parser(name, help=f'{name.split("-")[0].upper()} delta between two captures')
        sp.add_argument('--from', dest='from_ts', default='prev',
                        help="prev | latest | YYYY-MM-DD_HHMM | ISO timestamp (default: prev)")
        sp.add_argument('--to', dest='to_ts', default='latest',
                        help="prev | latest | YYYY-MM-DD_HHMM | ISO timestamp (default: latest)")
        sp.add_argument('--symbol', help='filter to one ticker (e.g. TSLA)')
        sp.add_argument('--min-dte', type=int,
                        help='filter contracts with DTE >= N (iv-change default 1, oi-change default 0)')
        sp.add_argument('--type', choices=['C', 'P', 'c', 'p'], help='calls only or puts only')
        sp.add_argument('--top', type=int, default=20, help='number of rows to show (default 20)')
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not DB_PATH.exists():
        sys.exit(f'no DB at {DB_PATH}')
    conn = sqlite3.connect(DB_PATH)
    try:
        dispatch = {
            'list-captures': cmd_list_captures,
            'daily-summary': cmd_daily_summary,
            'iv-change': cmd_iv_change,
            'oi-change': cmd_oi_change,
            'report': cmd_report,
        }
        dispatch[args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
