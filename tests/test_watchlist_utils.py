from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import watchlist_utils as wl


def test_default_watchlist_loads_expected_symbols() -> None:
    rows = wl.load_watchlist()

    symbols = [row.symbol for row in rows]
    assert symbols == ["TVSMOTOR", "RELIANCE", "HDFCBANK", "INFY", "TATAMOTORS"]
    assert rows[0].fyers_symbol == "NSE:TVSMOTOR-EQ"
    assert rows[0].company == "TVS Motor Company"
    assert rows[0].sector == "Auto"
    assert rows[0].basket == "core"


def test_fyers_symbols_helper_returns_fyers_column() -> None:
    assert wl.fyers_symbols() == [
        "NSE:TVSMOTOR-EQ",
        "NSE:RELIANCE-EQ",
        "NSE:HDFCBANK-EQ",
        "NSE:INFY-EQ",
        "NSE:TATAMOTORS-EQ",
    ]


def test_loader_skips_blank_comment_and_missing_fyers_rows(tmp_path) -> None:
    csv_path = tmp_path / "wl.csv"
    csv_path.write_text(
        "# a leading comment\n"
        "\n"
        "symbol,fyers_symbol,company,sector,basket,notes\n"
        "GOOD,NSE:GOOD-EQ,Good Co,IT,core,keep me\n"
        "# COMMENT,NSE:SKIP-EQ,Comment Co,IT,core,drop comment row\n"
        "NOSYM,,No Fyers Symbol,IT,core,drop because blank fyers\n"
        "  \n"
        "GOOD2, NSE:GOOD2-EQ ,Good Two,Auto,satellite,trims whitespace\n",
        encoding="utf-8",
    )

    rows = wl.load_watchlist(csv_path)

    assert [row.symbol for row in rows] == ["GOOD", "GOOD2"]
    # whitespace around the fyers symbol is stripped
    assert rows[1].fyers_symbol == "NSE:GOOD2-EQ"


def test_loader_raises_for_missing_required_columns(tmp_path) -> None:
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("symbol,company\nGOOD,Good Co\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required columns"):
        wl.load_watchlist(csv_path)


def test_loader_raises_for_missing_file(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        wl.load_watchlist(tmp_path / "does-not-exist.csv")
