#!/usr/bin/env python3
"""Fetch FYERS v3 option-chain snapshots and store them in PostgreSQL.

Read-only market data only. No order placement. For each configured underlying
this calls the FYERS `optionchain` endpoint (with greeks), normalizes the
strike-by-strike rows, appends them to `market.option_chain_snapshots`, and writes
one aggregate row (PCR / max pain / ATM IV / IV regime) to
`market.option_chain_summary`. The chain math is delegated to the shared, pure
`option_chain_signals` module so the engine and dashboard agree on the numbers.

Example:
  python scripts/ingest_fyers_optionchain.py \
    --underlyings NSE:NIFTYBANK-INDEX NSE:NIFTY50-INDEX --strikecount 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.option_chain_signals import (  # noqa: E402
    ChainRow,
    _to_decimal,
    _to_int,
    classify_iv_regime,
    compute_atm_iv,
    compute_max_pain,
    compute_pcr,
    nearest_strike,
    total_oi,
)

load_dotenv()

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "options_chain.json"

# Map a FYERS index symbol to the short underlying label we store.
UNDERLYING_LABELS = {
    "NSE:NIFTYBANK-INDEX": "BANKNIFTY",
    "NSE:NIFTY50-INDEX": "NIFTY",
}


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


def underlying_label(underlying_symbol: str) -> str:
    if underlying_symbol in UNDERLYING_LABELS:
        return UNDERLYING_LABELS[underlying_symbol]
    # Fallback: strip exchange + -INDEX suffix, e.g. NSE:FOO-INDEX -> FOO.
    name = underlying_symbol.split(":", 1)[-1]
    return name.replace("-INDEX", "").upper()


def epoch_to_date(value: Any) -> date | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).date()
    except Exception:
        return None


def parse_chain_expiry(data: dict[str, Any]) -> date | None:
    """Best-effort current-expiry date from the FYERS optionchain `expiryData`."""
    expiry_data = data.get("expiryData") or []
    if isinstance(expiry_data, list) and expiry_data:
        first = expiry_data[0]
        if isinstance(first, dict):
            return epoch_to_date(first.get("expiry") or first.get("date"))
    return None


def normalize_chain_response(
    underlying_symbol: str, response: dict[str, Any]
) -> tuple[list[tuple[ChainRow, dict[str, Any]]], Decimal | None, date | None]:
    """Pure parse of a FYERS optionchain response into (rows+raw, spot, expiry).

    No network/DB. The single index row (strike_price == -1, empty option_type)
    is skipped but supplies the spot price. FYERS field names vary by build, so
    every field is resolved with a multi-key fallback like the quotes ingester."""
    data = response.get("data") or {}
    chain = data.get("optionsChain") or data.get("optionChain") or []
    expiry = parse_chain_expiry(data)
    label = underlying_label(underlying_symbol)

    rows: list[tuple[ChainRow, dict[str, Any]]] = []
    spot: Decimal | None = None
    for item in chain:
        if not isinstance(item, dict):
            continue
        strike = _to_decimal(first_present(item, "strike_price", "strike"))
        option_type = (first_present(item, "option_type", "optionType") or "").strip().upper()
        if strike is None or strike < 0 or option_type not in {"CE", "PE"}:
            # Index/underlying row: capture spot, then skip.
            ltp = _to_decimal(first_present(item, "ltp", "fp", "fyfp"))
            if spot is None and ltp is not None:
                spot = ltp
            continue
        row = ChainRow(
            underlying=label,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            symbol=first_present(item, "symbol", "fyToken"),
            ltp=_to_decimal(first_present(item, "ltp", "fp")),
            bid=_to_decimal(first_present(item, "bid", "bid_price", "bp")),
            ask=_to_decimal(first_present(item, "ask", "ask_price", "ap")),
            volume=_to_int(first_present(item, "volume", "vol", "v")),
            oi=_to_int(first_present(item, "oi", "openInterest", "open_interest")),
            oi_change=_to_int(first_present(item, "oich", "oi_change", "oichange")),
            delta=_to_decimal(first_present(item, "delta")),
            gamma=_to_decimal(first_present(item, "gamma")),
            theta=_to_decimal(first_present(item, "theta")),
            vega=_to_decimal(first_present(item, "vega")),
            iv=_to_decimal(first_present(item, "iv", "impliedVolatility", "implied_volatility")),
        )
        rows.append((row, item))
    return rows, spot, expiry


def build_summary(
    underlying_symbol: str,
    rows: list[ChainRow],
    spot: Decimal | None,
    expiry: date | None,
    iv_history: list[Decimal] | None = None,
) -> dict[str, Any]:
    """Aggregate one option-chain snapshot into a summary row payload."""
    atm_iv = compute_atm_iv(rows, spot)
    return {
        "underlying": underlying_label(underlying_symbol),
        "underlying_symbol": underlying_symbol,
        "expiry": expiry,
        "spot": spot,
        "atm_strike": nearest_strike(rows, spot) if spot is not None else None,
        "total_ce_oi": total_oi(rows, "CE"),
        "total_pe_oi": total_oi(rows, "PE"),
        "pcr": compute_pcr(rows),
        "max_pain_strike": compute_max_pain(rows),
        "atm_iv": atm_iv,
        "iv_regime": classify_iv_regime(atm_iv, iv_history),
    }


def recent_atm_iv_history(cur: psycopg.Cursor, underlying: str, limit: int = 60) -> list[Decimal]:
    cur.execute(
        """
        select atm_iv from market.option_chain_summary
        where underlying = %s and atm_iv is not null
        order by snapshot_time desc
        limit %s
        """,
        (underlying, limit),
    )
    out: list[Decimal] = []
    for (value,) in cur.fetchall():
        dec = _to_decimal(value)
        if dec is not None:
            out.append(dec)
    return out


def ingest_underlying(
    cur: psycopg.Cursor,
    underlying_symbol: str,
    response: dict[str, Any],
    snapshot_time: datetime,
) -> int:
    """Normalize one FYERS response and append snapshot + summary rows. Returns row count."""
    rows_with_raw, spot, expiry = normalize_chain_response(underlying_symbol, response)
    label = underlying_label(underlying_symbol)
    for row, raw in rows_with_raw:
        cur.execute(
            """
            insert into market.option_chain_snapshots(
                underlying, underlying_symbol, snapshot_time, expiry, strike, option_type,
                symbol, ltp, bid, ask, volume, oi, oi_change,
                delta, gamma, theta, vega, iv, raw)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                row.underlying, underlying_symbol, snapshot_time, row.expiry, row.strike, row.option_type,
                row.symbol, row.ltp, row.bid, row.ask, row.volume, row.oi, row.oi_change,
                row.delta, row.gamma, row.theta, row.vega, row.iv, json.dumps(raw),
            ),
        )
    iv_history = recent_atm_iv_history(cur, label)
    summary = build_summary(underlying_symbol, [r for r, _ in rows_with_raw], spot, expiry, iv_history)
    cur.execute(
        """
        insert into market.option_chain_summary(
            underlying, underlying_symbol, snapshot_time, expiry, spot, atm_strike,
            total_ce_oi, total_pe_oi, pcr, max_pain_strike, atm_iv, iv_regime, raw)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            summary["underlying"], underlying_symbol, snapshot_time, summary["expiry"], summary["spot"],
            summary["atm_strike"], summary["total_ce_oi"], summary["total_pe_oi"], summary["pcr"],
            summary["max_pain_strike"], summary["atm_iv"], summary["iv_regime"],
            json.dumps({k: str(v) if isinstance(v, Decimal) else v
                        for k, v in summary.items() if k not in {"expiry"}}, default=str),
        ),
    )
    return len(rows_with_raw)


def run_ingest(underlyings: list[str], strikecount: int) -> None:
    api = fyers()
    snapshot_time = datetime.now(timezone.utc)
    request_log = {"underlyings": underlyings, "strikecount": strikecount}
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into market.ingestion_runs(source, job_type, params)
                values ('fyers_v3', 'optionchain', %s::jsonb)
                returning run_id
                """,
                (json.dumps(request_log),),
            )
            run_id = cur.fetchone()[0]
            rows = 0
            try:
                for underlying_symbol in underlyings:
                    request = {"symbol": underlying_symbol, "strikecount": strikecount, "greeks": "1"}
                    response = api.optionchain(data=request)
                    if response.get("s") != "ok":
                        raise RuntimeError(f"FYERS optionchain failed for {underlying_symbol}: {response}")
                    rows += ingest_underlying(cur, underlying_symbol, response, snapshot_time)
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
                    (str(exc), rows, run_id),
                )
                raise
    print(f"Stored {rows} option-chain rows across {len(underlyings)} underlying(s)")


def load_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlyings", nargs="+", help="FYERS index symbols, e.g. NSE:NIFTYBANK-INDEX")
    parser.add_argument("--strikecount", type=int, help="Strikes each side of ATM (default from config)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()

    config = load_config(args.config)
    underlyings = args.underlyings or config.get("underlyings") or ["NSE:NIFTYBANK-INDEX"]
    strikecount = args.strikecount or int(config.get("strikecount", 10))
    run_ingest(underlyings, strikecount)


if __name__ == "__main__":
    main()
