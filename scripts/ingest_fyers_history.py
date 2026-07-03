#!/usr/bin/env python3
"""Ingest FYERS v3 historical candles into PostgreSQL.

Examples:
  python scripts/ingest_fyers_history.py --symbols NSE:SBIN-EQ NSE:RELIANCE-EQ --resolution D --from 2024-01-01 --to 2024-12-31
  python scripts/ingest_fyers_history.py --symbols NSE:NIFTY50-INDEX --resolution 5 --from 2026-01-01 --to 2026-01-02
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

import psycopg
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

load_dotenv()

ALLOWED_RESOLUTIONS = {"1", "2", "3", "5", "10", "15", "20", "30", "45", "60", "120", "240", "D", "W", "M"}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing {name}. Add it to /opt/data/finance-db/.env")
    return value


def connect_db() -> psycopg.Connection:
    return psycopg.connect(os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker"))


def fyers() -> fyersModel.FyersModel:
    return fyersModel.FyersModel(
        client_id=require_env("FYERS_CLIENT_ID"),
        token=require_env("FYERS_ACCESS_TOKEN"),
        log_path=os.getenv("FYERS_LOG_PATH", "/opt/data/finance-db/logs"),
        is_async=False,
    )


def parse_epoch(epoch: int | float | str) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def parse_iso_date_arg(value: str, flag: str) -> datetime:
    if (
        len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or not value[:4].isdigit()
        or not value[5:7].isdigit()
        or not value[8:10].isdigit()
    ):
        raise SystemExit(f"{flag} must be YYYY-MM-DD")
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"{flag} must be YYYY-MM-DD") from exc


def validate_ingest_args(resolution: str, range_from: str, range_to: str) -> None:
    if not resolution.strip():
        raise SystemExit("--resolution must be non-empty")
    if resolution not in ALLOWED_RESOLUTIONS:
        raise SystemExit(f"--resolution must be one of: {', '.join(sorted(ALLOWED_RESOLUTIONS))}")
    start = parse_iso_date_arg(range_from, "--from")
    end = parse_iso_date_arg(range_to, "--to")
    if start > end:
        raise SystemExit("--from must be on or before --to")


def upsert_instrument(cur: psycopg.Cursor, symbol: str, raw: dict[str, Any] | None = None) -> None:
    exchange = symbol.split(":", 1)[0] if ":" in symbol else None
    cur.execute(
        """
        insert into market.instruments(symbol, exchange, raw, updated_at)
        values (%s, %s, %s::jsonb, now())
        on conflict(symbol) do update set
            exchange = excluded.exchange,
            raw = coalesce(excluded.raw, market.instruments.raw),
            updated_at = now()
        """,
        (symbol, exchange, json.dumps(raw) if raw is not None else None),
    )


def run_ingest(symbols: list[str], resolution: str, range_from: str, range_to: str, cont_flag: str) -> None:
    validate_ingest_args(resolution, range_from, range_to)
    api = fyers()
    rows_inserted = 0
    rows_updated = 0
    params = {
        "symbols": symbols,
        "resolution": resolution,
        "range_from": range_from,
        "range_to": range_to,
        "cont_flag": cont_flag,
    }

    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into market.ingestion_runs(source, job_type, params)
                values ('fyers_v3', 'history', %s::jsonb)
                returning run_id
                """,
                (json.dumps(params),),
            )
            run_id = cur.fetchone()[0]

            try:
                for symbol in symbols:
                    request = {
                        "symbol": symbol,
                        "resolution": resolution,
                        "date_format": "1",
                        "range_from": range_from,
                        "range_to": range_to,
                        "cont_flag": cont_flag,
                    }
                    response = api.history(data=request)
                    status = response.get("s")
                    if status != "ok":
                        raise RuntimeError(f"FYERS history failed for {symbol}: {response}")

                    upsert_instrument(cur, symbol, {"last_history_response_status": status})
                    candles = response.get("candles") or []
                    for candle in candles:
                        ts, open_, high, low, close, volume = candle[:6]
                        cur.execute(
                            """
                            insert into market.candles(symbol, resolution, ts, open, high, low, close, volume, source, raw)
                            values (%s, %s, %s, %s, %s, %s, %s, %s, 'fyers_v3', %s::jsonb)
                            on conflict(symbol, resolution, ts) do update set
                                open = excluded.open,
                                high = excluded.high,
                                low = excluded.low,
                                close = excluded.close,
                                volume = excluded.volume,
                                raw = excluded.raw,
                                inserted_at = now()
                            """,
                            (
                                symbol,
                                resolution,
                                parse_epoch(ts),
                                open_, high, low, close, volume,
                                json.dumps(candle),
                            ),
                        )
                        if cur.rowcount == 1:
                            rows_inserted += 1
                    print(f"{symbol}: stored {len(candles)} candles")

                cur.execute(
                    """
                    update market.ingestion_runs
                    set finished_at = now(), status = 'success', rows_inserted = %s, rows_updated = %s
                    where run_id = %s
                    """,
                    (rows_inserted, rows_updated, run_id),
                )
            except Exception as exc:
                cur.execute(
                    """
                    update market.ingestion_runs
                    set finished_at = now(), status = 'error', notes = %s, rows_inserted = %s, rows_updated = %s
                    where run_id = %s
                    """,
                    (str(exc), rows_inserted, rows_updated, run_id),
                )
                raise

    print(f"Done. Rows processed: {rows_inserted + rows_updated}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True, help="FYERS symbols, e.g. NSE:SBIN-EQ")
    parser.add_argument("--resolution", default="D", help="FYERS resolution: 1, 5, 15, 60, D, W, M")
    parser.add_argument("--from", dest="range_from", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="range_to", required=True, help="YYYY-MM-DD")
    parser.add_argument("--cont-flag", default="1")
    args = parser.parse_args()
    run_ingest(args.symbols, args.resolution, args.range_from, args.range_to, args.cont_flag)


if __name__ == "__main__":
    main()
