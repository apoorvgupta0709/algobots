from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.nse_intraday_options_strategy_pack import (
    Candle,
    StrategyPackConfig,
    build_default_config,
    check_debit_spread_risk,
    evaluate_cpr_trend_debit_spread,
    evaluate_expiry_tuesday_directional,
    evaluate_nifty_orb_debit_spread,
    evaluate_nifty_vwap_mean_reversion,
    evaluate_single_stock_momentum,
    next_tuesday_expiry,
    strict_bool,
)


BASE_DAY = date(2026, 6, 9)  # Tuesday


def c(hhmm: str, o: str, h: str, l: str, cl: str, vol: int = 1000) -> Candle:
    hh, mm = map(int, hhmm.split(":"))
    return Candle(
        ts=datetime.combine(BASE_DAY, time(hh, mm)),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(cl),
        volume=vol,
    )


def make_orb_breakout_day(direction: str = "long") -> list[Candle]:
    rows = [
        c("09:15", "10000", "10020", "9980", "10000", 1000),
        c("09:20", "10000", "10030", "9985", "10010", 1000),
        c("09:25", "10010", "10040", "9990", "10020", 1000),
        c("09:30", "10020", "10040", "10000", "10030", 1000),
        c("09:35", "10030", "10040", "10010", "10030", 1000),
        c("09:40", "10030", "10040", "10010", "10030", 1000),
    ]
    if direction == "long":
        rows.append(c("09:45", "10030", "10090", "10030", "10070", 2200))
    else:
        rows.append(c("09:45", "10030", "10030", "9950", "9970", 2200))
    return rows


def test_default_config_is_paper_only_and_allocates_50000_per_strategy():
    cfg = build_default_config()

    assert cfg.paper_only is True
    assert cfg.live_orders_enabled is False
    assert set(cfg.strategies) == {
        "nifty_orb_debit_spread",
        "cpr_trend_debit_spread",
        "expiry_tuesday_directional",
        "nifty_vwap_mean_reversion",
        "single_stock_momentum_index_confirm",
    }
    assert all(s.paper_trade_enabled for s in cfg.strategies.values())
    assert all(s.paper_capital == Decimal("50000") for s in cfg.strategies.values())
    assert all(s.max_trade_loss == Decimal("1500") for s in cfg.strategies.values())


@pytest.mark.parametrize("value", ["maybe", "yes", 1, None])
def test_strict_bool_rejects_ambiguous_safety_values(value):
    with pytest.raises(ValueError):
        strict_bool(value, key="paper_only")


def test_debit_spread_risk_blocks_credit_or_too_large_debit():
    assert check_debit_spread_risk(Decimal("22.50"), lot_size=65, max_loss=Decimal("1500"))
    assert not check_debit_spread_risk(Decimal("23.10"), lot_size=65, max_loss=Decimal("1500"))
    assert not check_debit_spread_risk(Decimal("-2.00"), lot_size=65, max_loss=Decimal("1500"))


def test_nifty_orb_debit_spread_triggers_long_after_volume_confirmed_breakout():
    signal = evaluate_nifty_orb_debit_spread(
        make_orb_breakout_day("long"),
        vix=Decimal("15"),
        net_debit_per_share=Decimal("22"),
        lot_size=65,
    )

    assert signal is not None
    assert signal.strategy_id == "nifty_orb_debit_spread"
    assert signal.direction == "long"
    assert signal.structure == "bull_call_debit_spread"
    assert signal.max_loss_rupees <= Decimal("1500")


def test_cpr_trend_debit_spread_requires_narrow_cpr_and_prevday_break():
    prev = [
        c("15:15", "100", "100.3", "99.7", "100", 1000),
    ]
    today = [
        c("09:15", "100", "100.4", "99.9", "100.25", 1000),
        c("09:45", "100.3", "101.2", "100.2", "100.8", 1800),
    ]

    signal = evaluate_cpr_trend_debit_spread(
        today,
        previous_day=prev,
        underlying="NIFTY",
        vix=Decimal("16"),
        net_debit_per_share=Decimal("22"),
        lot_size=65,
        sessions_to_expiry=10,
    )

    assert signal is not None
    assert signal.strategy_id == "cpr_trend_debit_spread"
    assert signal.direction == "long"
    assert signal.structure == "bull_call_debit_spread"


def test_expiry_tuesday_directional_triggers_only_on_tuesday_before_cutoff():
    signal = evaluate_expiry_tuesday_directional(
        make_orb_breakout_day("long"),
        trade_date=BASE_DAY,
        vix=Decimal("18"),
        option_premium=Decimal("80"),
        lot_size=65,
    )

    assert signal is not None
    assert signal.strategy_id == "expiry_tuesday_directional"
    assert signal.direction == "long"
    assert signal.structure == "long_atm_ce"
    assert signal.stop_loss_rupees <= Decimal("1500")

    not_tuesday = evaluate_expiry_tuesday_directional(
        make_orb_breakout_day("long"),
        trade_date=BASE_DAY + timedelta(days=1),
        vix=Decimal("18"),
        option_premium=Decimal("80"),
        lot_size=65,
    )
    assert not_tuesday is None


def test_vwap_mean_reversion_triggers_ce_on_lower_band_rejection_range_day():
    rows = [
        c("09:50", "100", "101", "99", "100", 1000),
        c("09:55", "100", "100.5", "98", "99", 1000),
        c("10:00", "99", "99.5", "94", "98", 1800),  # lower wick + close > open-like recovery not enough by itself
        c("10:05", "96", "99", "93", "98.5", 2200),
    ]

    signal = evaluate_nifty_vwap_mean_reversion(
        rows,
        is_range_day=True,
        is_cpr_narrow=False,
        vix=Decimal("15"),
        rsi9=Decimal("38"),
        option_premium=Decimal("90"),
        lot_size=65,
    )

    assert signal is not None
    assert signal.strategy_id == "nifty_vwap_mean_reversion"
    assert signal.direction == "long_ce"
    assert signal.structure in {"long_atm_ce", "tight_debit_spread_ce"}


def test_single_stock_momentum_requires_stock_breakout_index_confirmation_and_rs():
    stock_rows = make_orb_breakout_day("long")
    index_rows = make_orb_breakout_day("long")

    signal = evaluate_single_stock_momentum(
        stock_rows,
        index_rows,
        stock_symbol="HDFCBANK",
        confirming_index="BANKNIFTY",
        vix=Decimal("16"),
        option_spread_pct=Decimal("0.003"),
        net_debit_per_share=Decimal("2.50"),
        lot_size=550,
        earnings_today=False,
        stock_intraday_pct=Decimal("0.80"),
        index_intraday_pct=Decimal("0.40"),
    )

    assert signal is not None
    assert signal.strategy_id == "single_stock_momentum_index_confirm"
    assert signal.direction == "long"
    assert signal.structure == "stock_option_debit_spread"
    assert signal.max_loss_rupees <= Decimal("1500")


def test_next_tuesday_expiry_returns_same_day_for_tuesday_else_next_tuesday():
    assert next_tuesday_expiry(date(2026, 6, 9)) == date(2026, 6, 9)
    assert next_tuesday_expiry(date(2026, 6, 10)) == date(2026, 6, 16)
