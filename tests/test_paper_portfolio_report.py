from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import run_paper_portfolio_report as report


def test_money_formats_inr() -> None:
    assert report.money(Decimal("5000")) == "₹5,000.00"
    assert report.money(Decimal("12.345")) == "₹12.35"


def test_compute_equity_adds_realized_and_unrealized() -> None:
    assert report.compute_equity(Decimal("5000"), Decimal("125.50"), Decimal("-25")) == Decimal("5100.50")
