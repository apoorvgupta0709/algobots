#!/usr/bin/env python3
"""Rebuild index 5-min candles from DhanHQ option data spot values.

Dhan option candles have a `spot` column recording the underlying index level
at each 5-min boundary. We deduplicate by ts and insert as index candles into
market.candles for the period 2020-2025 (before FYERS data starts).

Usage:
    uv run python scripts/rebuild_index_candles_from_dhan.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(".env")

import psycopg

SYMBOL_MAP = {
    13: "NSE:NIFTY50-INDEX",
    25: "NSE:NIFTYBANK-INDEX",
}
RESOLUTION = "5"
BATCH = 5000


def connect_db():
    url = os.getenv("DATABASE_URL") or "postgresql://hermes@127.0.0.1:55432/finance_tracker"
    return psycopg.connect(url, options="-c timezone=Asia/Kolkata")


def get_existing_range(conn, symbol: str) -> tuple[date | None, date | None]:
    """Get the date range already in market.candles for this symbol + 5min."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(ts)::date, max(ts)::date FROM market.candles WHERE symbol=%s AND resolution=%s",
            (symbol, RESOLUTION),
        )
        row = cur.fetchone()
        return row if row and row[0] else (None, None)


def rebuild_for_security(conn, security_id: int):
    """Rebuild index candles for one security from dhan_option_candles spot data."""
    symbol = SYMBOL_MAP[security_id]
    print(f"\n{'='*60}")
    print(f"Rebuilding {symbol} (security_id={security_id})...")

    # Check existing data range
    existing_start, existing_end = get_existing_range(conn, symbol)
    if existing_start:
        print(f"  Existing data: {existing_start} to {existing_end}")
        # Only insert data before existing_start and after existing_end
        conditions = []
        # Pre-FYERS: anything before existing_start
        if existing_start:
            conditions.append(f"AND ts::date < '{existing_start}'::date")
        # Post-FYERS: anything after existing_end (if we're extending forward)
        # But Dhan data only goes to 2025-07-07, so this won't apply
    else:
        print(f"  No existing data for {symbol}")
        conditions = [""]

    where_clause = conditions[0] if conditions else ""

    # Count rows to process
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT count(*) FROM (
                SELECT DISTINCT ts, spot::numeric
                FROM market.dhan_option_candles
                WHERE security_id = %s
                  AND interval_minutes = 5
                  AND spot IS NOT NULL AND spot > 0
                  {where_clause}
            ) t
        """, (security_id,))
        total_ts = cur.fetchone()[0]
        print(f"  Distinct timestamps to process: {total_ts}")

    # Get distinct spot readings per timestamp, ordered
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT ts, spot::numeric
            FROM market.dhan_option_candles
            WHERE security_id = %s
              AND interval_minutes = 5
              AND spot IS NOT NULL AND spot > 0
              {where_clause}
            GROUP BY ts, spot
            ORDER BY ts
        """, (security_id,))
        rows = cur.fetchall()

    if not rows:
        print("  No data found!")
        return 0

    # Build 5-min candles from sequential spot readings
    candles = []
    prev_ts = None
    prev_spot = None
    for ts, spot in rows:
        # Convert to IST-naive if needed (timestamp may be timezone-aware)
        if hasattr(ts, 'tzinfo') and ts.tzinfo:
            candle_ts = ts.astimezone(psycopg.types.datetime.timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)
        else:
            candle_ts = ts
        candle_open = prev_spot if prev_spot is not None else spot
        candle_close = spot
        candle_high = max(candle_open, spot)
        candle_low = min(candle_open, spot)
        volume = 0

        candles.append((symbol, RESOLUTION, candle_ts, candle_open, candle_high, candle_low, candle_close, volume))
        prev_spot = spot
        prev_ts = ts

    print(f"  Built {len(candles)} candles")

    # Insert in batches using executemany with ON CONFLICT DO NOTHING
    inserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(candles), BATCH):
            batch = candles[i:i + BATCH]
            # Build VALUES with %s placeholders for each column
            placeholders = ",".join(f"({','.join(['%s']*8)})" for _ in batch)
            params = []
            for sym, res, ts, o, h, l, c, vol in batch:
                params.extend([sym, res, ts, float(o), float(h), float(l), float(c), vol])
            
            sql = f"""
                INSERT INTO market.candles (symbol, resolution, ts, open, high, low, close, volume)
                VALUES {placeholders}
                ON CONFLICT (symbol, resolution, ts) DO NOTHING
            """
            cur.execute(sql, params)
            conn.commit()
            inserted += len(batch)
            print(f"    Inserted {inserted}/{len(candles)}", end="\r")

    print(f"\n  Done: {inserted} candles inserted for {symbol}")
    return inserted


def main():
    conn = connect_db()
    total = 0

    for sec_id in [13, 25]:
        count = rebuild_for_security(conn, sec_id)
        total += count

    conn.close()
    print(f"\n{'='*60}")
    print(f"Total candles inserted: {total}")
    print("Done.")


if __name__ == "__main__":
    main()