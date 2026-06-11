from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_banknifty_pullback_v2_backtest import Candle, next_minute_open


def minute_candle(ts: datetime) -> Candle:
    return Candle(ts=ts, open=Decimal("100"), high=Decimal("101"), low=Decimal("99"), close=Decimal("100"), volume=10)


def test_entry_fills_only_after_the_signal_candle_completes() -> None:
    base = datetime(2026, 6, 11, 10, 0)
    minute_rows = [minute_candle(base + timedelta(minutes=i)) for i in range(10)]
    signal_candle_start = base  # 5m candle spans 10:00-10:05; its data is known at 10:05

    entry = next_minute_open(minute_rows, signal_candle_start + timedelta(minutes=5))

    assert entry is not None
    # The fill must not precede the signal candle's close: 10:01-10:04 opens are look-ahead.
    assert entry.ts == base + timedelta(minutes=5)


def test_no_entry_when_session_ends_before_signal_candle_completes() -> None:
    base = datetime(2026, 6, 11, 15, 25)
    minute_rows = [minute_candle(base + timedelta(minutes=i)) for i in range(4)]

    assert next_minute_open(minute_rows, base + timedelta(minutes=5)) is None
