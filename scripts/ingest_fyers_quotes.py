#!/usr/bin/env python3
"""Fetch FYERS v3 quotes and upsert latest quote snapshots into PostgreSQL.

Example:
  python scripts/ingest_fyers_quotes.py --symbols NSE:SBIN-EQ NSE:RELIANCE-EQ
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


def first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def as_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except Exception:
        return None


def _redact(text: str, *, limit: int = 500) -> str:
    """Scrub known secret env values out of a note string and cap its length.

    Run notes are persisted to PostgreSQL and printed, so they must never carry
    tokens / client ids / secrets that might appear inside an exception message
    or a URL embedded in it. We replace any occurrence of a known credential
    value with a placeholder and truncate, so a leaked token never lands in the
    database.
    """
    out = text
    for name in (
        "FYERS_ACCESS_TOKEN",
        "FYERS_CLIENT_ID",
        "FYERS_SECRET_KEY",
        "FYERS_REFRESH_TOKEN",
    ):
        secret = os.getenv(name)
        if secret:
            out = out.replace(secret, f"<{name}>")
    if len(out) > limit:
        out = out[:limit] + "...(truncated)"
    return out


def _safe_response_note(response: dict[str, Any]) -> str:
    """Summarize a non-ok FYERS response WITHOUT echoing the raw payload.

    The full response dict can contain credential-bearing or otherwise sensitive
    fields, so only the status, error code, and a redacted human message are
    kept — never the raw body.
    """
    parts = [f"s={response.get('s')}"]
    code = response.get("code")
    if code is not None:
        parts.append(f"code={code}")
    message = response.get("message")
    if message:
        parts.append(f"message={_redact(str(message), limit=200)}")
    return "FYERS quotes non-ok response: " + ", ".join(parts)


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


def run_ingest(symbols: list[str]) -> None:
    api = fyers()
    request = {"symbols": ",".join(symbols)}
    # Create the ingestion run row BEFORE the FYERS call so that EVERY attempt —
    # including an auth/token failure that never returns ok — is recorded as a
    # run the readiness watchdog can see. The row starts as 'running' and is
    # updated to 'success' or 'error' below; on error we commit it explicitly so
    # the failed run survives the re-raise (the connection context manager would
    # otherwise roll the whole transaction, including this row, back).
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into market.ingestion_runs(source, job_type, params)
                values ('fyers_v3', 'quotes', %s::jsonb)
                returning run_id
                """,
                (json.dumps(request),),
            )
            run_id = cur.fetchone()[0]
            rows = 0
            try:
                response = api.quotes(data=request)
                if response.get("s") != "ok":
                    raise RuntimeError(_safe_response_note(response))
                quotes = response.get("d") or []
                for item in quotes:
                    symbol = item.get("n") or item.get("symbol")
                    v = item.get("v") or item
                    if not symbol:
                        continue
                    upsert_instrument(cur, symbol, item)
                    cur.execute(
                        """
                        insert into market.quotes(symbol, ltp, open, high, low, close, volume, quote_time, raw, updated_at)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                        on conflict(symbol) do update set
                            ltp = excluded.ltp,
                            open = excluded.open,
                            high = excluded.high,
                            low = excluded.low,
                            close = excluded.close,
                            volume = excluded.volume,
                            quote_time = excluded.quote_time,
                            raw = excluded.raw,
                            updated_at = now()
                        """,
                        (
                            symbol,
                            first_present(v, "lp", "ltp"),
                            first_present(v, "open_price", "open"),
                            first_present(v, "high_price", "high"),
                            first_present(v, "low_price", "low"),
                            # NOTE: "chp" is change-PERCENT, not a price — it
                            # must never be a fallback for close.
                            first_present(v, "prev_close_price", "close"),
                            first_present(v, "volume", "vol_traded_today"),
                            as_time(first_present(v, "tt", "exchange_timestamp", "cmd", "last_traded_time")),
                            json.dumps(item),
                        ),
                    )
                    rows += 1
                cur.execute(
                    """
                    update market.ingestion_runs
                    set finished_at = now(), status = 'success', rows_inserted = %s
                    where run_id = %s
                    """,
                    (rows, run_id),
                )
            except Exception as exc:
                cur.execute(
                    """
                    update market.ingestion_runs
                    set finished_at = now(), status = 'error', notes = %s, rows_inserted = %s
                    where run_id = %s
                    """,
                    (_redact(str(exc)), rows, run_id),
                )
                # Persist the failed run before re-raising; the context manager
                # rolls back on exception, which would otherwise discard it.
                conn.commit()
                raise
    print(f"Stored {rows} quote snapshots")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True, help="FYERS symbols, e.g. NSE:SBIN-EQ")
    args = parser.parse_args()
    run_ingest(args.symbols)


if __name__ == "__main__":
    main()
