from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.banknifty_options_paper import IST, load_config
from scripts.run_banknifty_pullback_v2_backtest import (
    DEFAULT_CONFIG,
    Candle,
    next_minute_open,
    simulate_trade,
    validate_experimental_inputs,
)


def _candle(minute: int, o: str, h: str, l: str, c: str) -> Candle:
    ts = datetime(2026, 6, 17, 10, minute, tzinfo=IST)
    return Candle(ts=ts, open=Decimal(o), high=Decimal(h), low=Decimal(l), close=Decimal(c), volume=1000)


def _run_synthetic_breakeven_trade(*, cost_aware_breakeven: bool):
    # Synthetic MFE path mirroring the Jun-17 CE trade: a +₹900 spike arms the 0.5R
    # breakeven lock, then price retraces back to entry and the lock fires.
    rows = [
        _candle(0, "50000", "50060", "50000", "50050"),
        _candle(1, "50050", "50050", "50000", "50000"),
        _candle(2, "50000", "50000", "49990", "50000"),
    ]
    config = load_config(DEFAULT_CONFIG)
    return simulate_trade(
        config=config,
        day=rows[0].ts.date(),
        direction="CE",
        signal_ts=rows[0].ts,
        entry_candle=rows[0],
        minute_rows=rows,
        reference_level=Decimal("49000"),
        index_stop=Decimal("49900"),
        rank="ATM",
        beta=Decimal("0.5"),
        daily_realized=Decimal("0"),
        round_trip_cost=Decimal("100"),
        cost_aware_breakeven=cost_aware_breakeven,
    )


def test_legacy_gross_breakeven_lets_a_protected_trade_net_negative_after_costs() -> None:
    trade = _run_synthetic_breakeven_trade(cost_aware_breakeven=False)
    assert trade is not None
    assert trade.exit_reason == "mfe_ratchet_stop"
    # Gross-flat breakeven nets ≈ minus the round-trip cost (the Jun-17 ₹-98.50 bug).
    assert trade.pnl_rupees == Decimal("-98.50")


def test_cost_aware_breakeven_protects_a_breakeven_trade_after_costs() -> None:
    trade = _run_synthetic_breakeven_trade(cost_aware_breakeven=True)
    assert trade is not None
    assert trade.exit_reason == "mfe_ratchet_stop"
    # Cost-aware lock keeps the protected trade at net breakeven (within one tick).
    assert trade.pnl_rupees >= Decimal("-5")
    assert trade.pnl_rupees > _run_synthetic_breakeven_trade(cost_aware_breakeven=False).pnl_rupees


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


def _valid_exit_kwargs(**overrides):
    base = {
        "breakeven_at_r": None,
        "ratchet_start_r": None,
        "ratchet_giveback_pct": None,
        "ratchet_giveback_min_inr": None,
        "round_trip_cost": Decimal("100"),
    }
    base.update(overrides)
    return base


def test_negative_round_trip_cost_is_rejected() -> None:
    with pytest.raises(SystemExit):
        validate_experimental_inputs(**_valid_exit_kwargs(round_trip_cost=Decimal("-1")))


def test_non_positive_exit_overrides_are_rejected() -> None:
    for key in ("breakeven_at_r", "ratchet_start_r", "ratchet_giveback_pct"):
        with pytest.raises(SystemExit):
            validate_experimental_inputs(**_valid_exit_kwargs(**{key: Decimal("0")}))
        with pytest.raises(SystemExit):
            validate_experimental_inputs(**_valid_exit_kwargs(**{key: Decimal("-1")}))

    with pytest.raises(SystemExit):
        validate_experimental_inputs(**_valid_exit_kwargs(ratchet_giveback_min_inr=Decimal("-5")))


def test_valid_experimental_inputs_pass() -> None:
    validate_experimental_inputs(
        breakeven_at_r=Decimal("0.5"),
        ratchet_start_r=Decimal("1.0"),
        ratchet_giveback_pct=Decimal("0.3"),
        ratchet_giveback_min_inr=Decimal("0"),
        round_trip_cost=Decimal("0"),
    )
