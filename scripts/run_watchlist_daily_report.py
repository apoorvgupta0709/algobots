#!/usr/bin/env python3
"""Refresh a watchlist and write a read-only daily technical report.

One command orchestrates the existing read-only building blocks:

1. (optional) ingest FYERS historical candles for the watchlist symbols,
2. (optional) ingest the latest FYERS quotes,
3. (optional) compute technical factor snapshots from stored candles,
4. render the Markdown daily market report.

This is decision-support infrastructure only. It never places, modifies, or
cancels orders, and it has no live execution path. The `--skip-*` flags exist
for retries and for running against already-ingested data without calling FYERS.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import compute_technical_factors as factors
from scripts import generate_daily_market_report as report
from scripts import ingest_fyers_history as history
from scripts import ingest_fyers_quotes as quotes
from scripts import watchlist_utils
DEFAULT_WATCHLIST = PROJECT_ROOT / "watchlists" / "default.csv"
# ~365 calendar days usually gives enough Indian-market trading candles for the
# advertised long-window factors, including SMA200.
DEFAULT_HISTORY_DAYS = 365
# Load generously so optional long-window factors compute when history allows.
FACTOR_LOOKBACK = 400
QUOTE_BATCH_SIZE = 50
ALLOWED_RESOLUTIONS = {"1", "2", "3", "5", "10", "15", "20", "30", "45", "60", "120", "240", "D", "W", "M"}


def chunks(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [values[idx: idx + size] for idx in range(0, len(values), size)]


def default_date_range(today: date | None = None) -> tuple[str, str]:
    """Return ``(range_from, range_to)`` strings for the default lookback window."""
    today = today or datetime.now(timezone.utc).date()
    range_from = today - timedelta(days=DEFAULT_HISTORY_DAYS)
    return range_from.isoformat(), today.isoformat()


def connect_db() -> psycopg.Connection:
    return psycopg.connect(report.DATABASE_URL)


def refresh_factors(symbols: list[str], resolution: str) -> int:
    """Compute/store factor snapshots, skipping symbols without enough candles.

    A watchlist can contain symbols whose history has not been ingested yet. The
    daily report should still run for symbols with valid stored candles rather
    than failing the whole batch. Missing-history symbols are printed as data
    gaps, not guessed.
    """
    snapshots: list[factors.FactorSnapshot] = []
    with connect_db() as conn:
        for symbol in symbols:
            candles = factors.fetch_candles(conn, symbol, resolution, FACTOR_LOOKBACK)
            try:
                snapshots.append(factors.compute_latest_snapshot(candles))
            except ValueError as exc:
                print(f"Skipping factor computation for {symbol}: {exc}")
        if not snapshots:
            return 0
        return factors.store_factor_snapshots(conn, snapshots)


def build_report(symbols: list[str], resolution: str, limit: int, output: Path | None) -> tuple[str, Path]:
    with connect_db() as conn:
        rows = report.fetch_report_rows(conn, symbols, limit, resolution)
    text = report.render_report(rows)
    path = report.write_report(text, output)
    return text, path


def parse_iso_date_arg(value: str, flag: str) -> date:
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
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"{flag} must be YYYY-MM-DD") from exc


def validate_args(args: argparse.Namespace) -> None:
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if not args.resolution.strip():
        raise SystemExit("--resolution must be non-empty")
    if args.resolution not in ALLOWED_RESOLUTIONS:
        raise SystemExit(f"--resolution must be one of: {', '.join(sorted(ALLOWED_RESOLUTIONS))}")
    default_from, default_to = default_date_range()
    range_from = parse_iso_date_arg(args.range_from or default_from, "--from")
    range_to = parse_iso_date_arg(args.range_to or default_to, "--to")
    if range_from and range_to and range_from > range_to:
        raise SystemExit("--from must be on or before --to")


def run(args: argparse.Namespace) -> Path:
    validate_args(args)
    rows = watchlist_utils.load_watchlist(args.watchlist)
    symbols = [row.fyers_symbol for row in rows]
    if not symbols:
        raise SystemExit(f"No FYERS symbols found in watchlist: {args.watchlist}")

    range_from = args.range_from
    range_to = args.range_to
    if range_from is None or range_to is None:
        default_from, default_to = default_date_range()
        range_from = range_from or default_from
        range_to = range_to or default_to

    if not args.skip_history:
        print(f"Ingesting history for {len(symbols)} symbols {range_from}..{range_to} ({args.resolution})")
        history.run_ingest(symbols, args.resolution, range_from, range_to, args.cont_flag)
    else:
        print("Skipping history ingestion")

    if not args.skip_quotes:
        print(f"Ingesting latest quotes for {len(symbols)} symbols")
        for batch in chunks(symbols, QUOTE_BATCH_SIZE):
            quotes.run_ingest(batch)
    else:
        print("Skipping quote ingestion")

    if not args.skip_factors:
        stored = refresh_factors(symbols, args.resolution)
        print(f"Stored {stored} technical factor snapshots")
    else:
        print("Skipping factor computation")

    text, path = build_report(symbols, args.resolution, args.limit, args.output)
    if args.print:
        print(text)
    print(f"Wrote daily market report: {path}")
    print("Read-only research report; no orders placed and no execution calls made.")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh a watchlist and write a read-only daily technical report (no orders placed)",
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="Watchlist CSV path")
    parser.add_argument("--resolution", default="D", help="FYERS candle resolution; default D")
    parser.add_argument("--from", dest="range_from", help="History start YYYY-MM-DD; default ~365 days before today")
    parser.add_argument("--to", dest="range_to", help="History end YYYY-MM-DD; default today")
    parser.add_argument("--cont-flag", default="1", help="FYERS cont_flag for history requests")
    parser.add_argument("--limit", type=int, default=25, help="Maximum report rows")
    parser.add_argument("--skip-history", action="store_true", help="Do not ingest FYERS candles")
    parser.add_argument("--skip-quotes", action="store_true", help="Do not ingest FYERS quotes")
    parser.add_argument("--skip-factors", action="store_true", help="Do not recompute technical factors")
    parser.add_argument("--output", type=Path, help="Report markdown path; defaults to reports/daily_market_report_YYYY-MM-DD.md")
    parser.add_argument("--print", action="store_true", help="Print report text to stdout as well as writing file")
    return parser


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
