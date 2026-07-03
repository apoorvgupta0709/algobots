#!/usr/bin/env python3
"""Capture FYERS read-only trading snapshots into PostgreSQL.

This script intentionally supports only read-only FYERS endpoints:
positions, orderbook, holdings, and funds. It contains no order placement,
modification, cancellation, or position-conversion calls.

Example:
  FYERS_LOG_PATH=/tmp/ uv run python scripts/ingest_fyers_trading_snapshots.py --resources positions holdings
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psycopg
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT_ROOT / "migrations" / "001_trading_research_schemas.sql"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class SnapshotSpec:
    name: str
    api_method: str
    table: str
    payload_column: str


@dataclass(frozen=True)
class SnapshotResult:
    name: str
    table: str
    payload_column: str
    payload: dict[str, Any]
    raw: dict[str, Any]


SNAPSHOT_SPECS: dict[str, SnapshotSpec] = {
    "positions": SnapshotSpec(
        name="positions",
        api_method="positions",
        table="trading.positions_snapshots",
        payload_column="positions",
    ),
    "orderbook": SnapshotSpec(
        name="orderbook",
        api_method="orderbook",
        table="trading.orderbook_snapshots",
        payload_column="orders",
    ),
    "holdings": SnapshotSpec(
        name="holdings",
        api_method="holdings",
        table="trading.holdings_snapshots",
        payload_column="holdings",
    ),
    "funds": SnapshotSpec(
        name="funds",
        api_method="funds",
        table="trading.funds_snapshots",
        payload_column="funds",
    ),
}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing {name}. Add it to {PROJECT_ROOT / '.env'}")
    return value


def normalize_log_path(value: str | None) -> str:
    """Return a writable FYERS SDK log path with the SDK's expected trailing slash."""
    candidates = [value, "/tmp/"] if value else ["/tmp/"]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".fyers-log-write-test"
            probe.write_text("ok")
            probe.unlink(missing_ok=True)

            # FYERS SDK opens exactly '<log_path>fyersApi.log'. A directory can be
            # writable while an existing SDK log file is not, so test that file too.
            sdk_log = path / "fyersApi.log"
            with sdk_log.open("a"):
                pass

            text = str(path)
            return text if text.endswith("/") else f"{text}/"
        except OSError:
            continue
    raise SystemExit("No writable FYERS log path found")


def connect_db() -> psycopg.Connection:
    return psycopg.connect(os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker"))


def fyers() -> fyersModel.FyersModel:
    return fyersModel.FyersModel(
        client_id=require_env("FYERS_CLIENT_ID"),
        token=require_env("FYERS_ACCESS_TOKEN"),
        log_path=normalize_log_path(os.getenv("FYERS_LOG_PATH")),
        is_async=False,
    )


def mask_account_ref(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 2:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}" if len(value) > 4 else f"{value[0]}{'*' * (len(value) - 1)}"


def payload_from_response(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key not in {"s", "code", "message"}}


def collect_snapshots(api: Any, resource_names: Iterable[str]) -> list[SnapshotResult]:
    results: list[SnapshotResult] = []
    for name in resource_names:
        spec = SNAPSHOT_SPECS[name]
        method = getattr(api, spec.api_method)
        response = method()
        if not isinstance(response, dict):
            raise RuntimeError(f"FYERS {name} returned non-dict response")
        if response.get("s") not in (None, "ok"):
            raise RuntimeError(f"FYERS {name} failed: status={response.get('s')!r}, code={response.get('code')!r}, message={response.get('message')!r}")
        results.append(
            SnapshotResult(
                name=name,
                table=spec.table,
                payload_column=spec.payload_column,
                payload=payload_from_response(response),
                raw=response,
            )
        )
    return results


def apply_schema(conn: psycopg.Connection) -> None:
    if not MIGRATION.exists():
        raise RuntimeError(f"Missing schema migration: {MIGRATION}")
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text())
    conn.commit()


def store_snapshots(conn: psycopg.Connection, snapshots: Iterable[SnapshotResult], account_ref: str | None) -> int:
    rows = 0
    with conn.cursor() as cur:
        for snapshot in snapshots:
            # Table and column names come only from SNAPSHOT_SPECS constants.
            cur.execute(
                f"""
                insert into {snapshot.table}(source, account_ref, {snapshot.payload_column}, raw)
                values ('fyers_v3', %s, %s::jsonb, %s::jsonb)
                """,
                (mask_account_ref(account_ref), json.dumps(snapshot.payload), json.dumps(snapshot.raw)),
            )
            rows += 1
    conn.commit()
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Store FYERS read-only trading snapshots")
    parser.add_argument(
        "--resources",
        nargs="+",
        choices=sorted(SNAPSHOT_SPECS),
        default=sorted(SNAPSHOT_SPECS),
        help="Read-only FYERS resources to snapshot",
    )
    parser.add_argument("--account-ref", help="Optional local account reference; stored masked only")
    parser.add_argument("--skip-schema", action="store_true", help="Do not apply the idempotent trading schema migration first")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api = fyers()
    snapshots = collect_snapshots(api, args.resources)
    with connect_db() as conn:
        if not args.skip_schema:
            apply_schema(conn)
        rows = store_snapshots(conn, snapshots, args.account_ref)
    print(f"Stored {rows} FYERS read-only trading snapshots: {', '.join(args.resources)}")


if __name__ == "__main__":
    main()
