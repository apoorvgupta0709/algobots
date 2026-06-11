#!/usr/bin/env python3
"""Load multi-symbol watchlists for the read-only technical report.

This is research/decision-support infrastructure only. It parses a CSV of
symbols and never places, modifies, or cancels orders.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WATCHLIST = PROJECT_ROOT / "watchlists" / "default.csv"

EXPECTED_COLUMNS = ("symbol", "fyers_symbol", "company", "sector", "basket", "notes")


@dataclass(frozen=True)
class WatchlistRow:
    symbol: str
    fyers_symbol: str
    company: str
    sector: str
    basket: str
    notes: str


def _clean(value: str | None) -> str:
    return (value or "").strip()


def load_watchlist(path: str | Path = DEFAULT_WATCHLIST) -> list[WatchlistRow]:
    """Return validated watchlist rows, skipping blank and comment lines.

    A row is kept only when it has a non-empty ``fyers_symbol``. Rows whose
    ``symbol`` begins with ``#`` are treated as comments and dropped, which lets
    the CSV carry human-readable documentation lines.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Watchlist file not found: {path}")

    rows: list[WatchlistRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        # Drop comment and blank lines before the CSV reader sees the header so
        # that documentation comments above the header do not break parsing.
        meaningful = [
            line
            for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        ]
        reader = csv.DictReader(meaningful)
        if reader.fieldnames is None:
            return rows
        missing = [col for col in EXPECTED_COLUMNS if col not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"Watchlist {path} is missing required columns: {', '.join(missing)}"
            )
        for record in reader:
            fyers_symbol = _clean(record.get("fyers_symbol"))
            symbol = _clean(record.get("symbol"))
            if symbol.startswith("#"):
                continue
            if not fyers_symbol:
                continue
            rows.append(
                WatchlistRow(
                    symbol=symbol,
                    fyers_symbol=fyers_symbol,
                    company=_clean(record.get("company")),
                    sector=_clean(record.get("sector")),
                    basket=_clean(record.get("basket")),
                    notes=_clean(record.get("notes")),
                )
            )
    return rows


def fyers_symbols(path: str | Path = DEFAULT_WATCHLIST) -> list[str]:
    """Convenience helper returning just the FYERS symbols from the watchlist."""
    return [row.fyers_symbol for row in load_watchlist(path)]
