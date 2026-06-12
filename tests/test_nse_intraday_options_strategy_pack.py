from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
import json
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.nse_intraday_options_strategy_pack import (
    Candle,
    StrategyPackConfig,
    build_default_config,
    check_debit_spread_risk,
    config_to_json_dict,
    evaluate_cpr_trend_debit_spread,
    evaluate_expiry_tuesday_directional,
    evaluate_nifty_orb_debit_spread,
    evaluate_nifty_vwap_mean_reversion,
    evaluate_single_stock_momentum,
    load_config,
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
        # Bias needs the full first 15 minutes (three 5m candles); the 09:25
        # candle's close is the first-15-minute close.
        c("09:15", "100", "100.4", "99.9", "100.2", 1000),
        c("09:20", "100.2", "100.35", "100.05", "100.22", 900),
        c("09:25", "100.22", "100.4", "100.1", "100.25", 950),
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


def test_orb_skips_formation_whipsaw_that_breached_both_sides_before_0945():
    rows = [
        c("09:15", "10000", "10020", "9980", "10000", 1000),
        c("09:20", "10000", "10025", "9985", "10010", 1000),
        c("09:25", "10010", "10030", "9990", "10020", 1000),
        c("09:30", "10020", "10045", "10000", "10030", 1000),  # above the 09:15-09:25 high
        c("09:35", "10030", "10035", "9960", "9990", 1000),  # below the 09:15-09:25 low
        c("09:45", "10030", "10090", "10030", "10070", 2200),  # would otherwise confirm long
    ]

    assert evaluate_nifty_orb_debit_spread(rows, vix=Decimal("15"), net_debit_per_share=Decimal("22"), lot_size=65) is None


def test_orb_skips_post_or_whipsaw_before_the_signal_candle():
    rows = [
        c("09:15", "10000", "10020", "9980", "10000", 1000),
        c("09:20", "10000", "10030", "9985", "10010", 1000),
        c("09:25", "10010", "10040", "9990", "10020", 1000),
        c("09:30", "10020", "10040", "10000", "10030", 1000),
        c("09:35", "10030", "10040", "10010", "10030", 1000),
        c("09:40", "10030", "10040", "10010", "10030", 1000),
        c("09:45", "10030", "10055", "10010", "10035", 800),  # pokes above OR high, closes back inside
        c("09:50", "10035", "10040", "9970", "10000", 900),  # pokes below OR low, closes back inside
        c("09:55", "10000", "10090", "10000", "10070", 2500),  # would otherwise confirm long
    ]

    assert evaluate_nifty_orb_debit_spread(rows, vix=Decimal("15"), net_debit_per_share=Decimal("22"), lot_size=65) is None


def test_single_stock_short_requires_relative_weakness_not_strength():
    stock_rows = make_orb_breakout_day("short")
    index_rows = make_orb_breakout_day("short")
    common = dict(
        stock_symbol="HDFCBANK",
        confirming_index="BANKNIFTY",
        vix=Decimal("16"),
        option_spread_pct=Decimal("0.003"),
        net_debit_per_share=Decimal("2.50"),
        lot_size=550,
        earnings_today=False,
    )

    # Stock holding up better than the falling index must not short.
    outperforming = evaluate_single_stock_momentum(
        stock_rows, index_rows, **common, stock_intraday_pct=Decimal("-0.10"), index_intraday_pct=Decimal("-0.40")
    )
    assert outperforming is None

    # Stock underperforming the index by >= 0.2% may short.
    underperforming = evaluate_single_stock_momentum(
        stock_rows, index_rows, **common, stock_intraday_pct=Decimal("-0.80"), index_intraday_pct=Decimal("-0.40")
    )
    assert underperforming is not None
    assert underperforming.direction == "short"


def test_load_config_rejects_string_booleans_for_strategy_flags(tmp_path: Path):
    data = config_to_json_dict(build_default_config())
    data["strategies"]["nifty_orb_debit_spread"]["paper_trade_enabled"] = "false"
    config_path = tmp_path / "pack.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(config_path)


def test_load_config_rejects_malformed_force_exit_time(tmp_path: Path):
    data = config_to_json_dict(build_default_config())
    data["force_exit_time"] = "25:99"
    config_path = tmp_path / "pack.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(config_path)


def test_debit_spread_proxy_payout_is_capped_at_structural_max_value():
    from scripts.run_nse_intraday_options_strategy_pack import simulate_proxy_trade

    signal = evaluate_nifty_orb_debit_spread(
        make_orb_breakout_day("long"), vix=Decimal("15"), net_debit_per_share=Decimal("22"), lot_size=65
    )
    assert signal is not None
    rows = make_orb_breakout_day("long") + [
        c("09:50", "10070", "10300", "10070", "10290", 3000),  # blasts far past any +2R target
    ]

    trade = simulate_proxy_trade(
        signal,
        rows,
        strategy_name="Nifty ORB Debit Spread",
        underlying="NIFTY",
        underlying_symbol="NSE:NIFTY50-INDEX",
        stop_pct=Decimal("0.0025"),
        target_r=Decimal("2"),
        time_exit=time(13, 45),
        cost_rupees=Decimal("120"),
    )

    assert trade is not None
    # 50-point spread bought for 22/share over 65 lot: max value (50-22)*65 = 1820
    # vs 1430 risk -> 1.27R structural ceiling; the +2R proxy payout is impossible.
    assert trade.target_r == Decimal("1.27")
    assert trade.pnl_r <= Decimal("1.27")


def test_tick_entry_guards_block_stale_signals_and_closed_windows():
    from scripts.run_nse_intraday_options_strategy_pack import entry_window_open, signal_is_fresh

    cfg = build_default_config()
    assert entry_window_open(datetime.combine(BASE_DAY, time(10, 0)), cfg)
    assert not entry_window_open(datetime.combine(BASE_DAY, time(9, 25)), cfg)
    assert not entry_window_open(datetime.combine(BASE_DAY, time(15, 20)), cfg)

    now = datetime.combine(BASE_DAY, time(13, 0))
    assert signal_is_fresh(now - timedelta(minutes=5), now)
    assert not signal_is_fresh(now - timedelta(hours=3), now)
    assert not signal_is_fresh(now + timedelta(minutes=5), now)


def test_orb_exit_at_structure_close_loses_less_than_full_debit():
    """Card exit: a 5m close back inside the OR exits proportionally, not at -1R."""
    from scripts.run_nse_intraday_options_strategy_pack import simulate_proxy_trade

    rows = [
        c("09:15", "10000", "10020", "9980", "10000", 1000),
        c("09:20", "10000", "10030", "9985", "10010", 1000),
        c("09:25", "10010", "10040", "9990", "10020", 1000),
        c("09:30", "10020", "10040", "10000", "10030", 1000),
        c("09:35", "10030", "10040", "10010", "10030", 1000),
        c("09:40", "10030", "10040", "10010", "10030", 1000),
        c("09:45", "10030", "10050", "10030", "10045", 2200),  # breakout close just above OR high 10040
        c("09:50", "10044", "10046", "10025", "10030", 900),  # closes back inside the OR; hard stop (10019.9) not hit
    ]
    signal = evaluate_nifty_orb_debit_spread(rows[:7], vix=Decimal("15"), net_debit_per_share=Decimal("22"), lot_size=65)
    assert signal is not None
    assert signal.metadata.get("structure_level") == "10040"

    trade = simulate_proxy_trade(
        signal,
        rows,
        strategy_name="Nifty ORB Debit Spread",
        underlying="NIFTY",
        underlying_symbol="NSE:NIFTY50-INDEX",
        stop_pct=Decimal("0.0025"),
        target_r=Decimal("2"),
        time_exit=time(13, 45),
        cost_rupees=Decimal("120"),
    )

    assert trade is not None
    assert trade.exit_reason == "structure_close_stop"
    # Proportional loss (~-0.6R) instead of the full -1R debit.
    assert Decimal("-0.95") < trade.pnl_r < Decimal("0")


def test_premium_stop_exits_before_full_debit_loss():
    from scripts.nse_intraday_options_strategy_pack import StrategySignal
    from scripts.run_nse_intraday_options_strategy_pack import simulate_proxy_trade

    signal = StrategySignal(
        strategy_id="cpr_trend_debit_spread",
        direction="long",
        structure="bull_call_debit_spread",
        entry_time=c("10:00", "10100", "10100", "10100", "10100").ts,
        underlying_entry=Decimal("10100"),
        reason="test",
        max_loss_rupees=Decimal("1430"),
        stop_loss_rupees=Decimal("1200"),
        target_r=Decimal("2"),
        metadata={"structure_level": "10000", "net_debit_per_share": "22", "lot_size": 65},
    )
    # Hard stop sits at 10074.75 (-1R); premium stop (-1200/1430 = -0.84R) is ~-21.2 points.
    rows = [
        c("10:00", "10100", "10100", "10100", "10100", 1000),
        c("10:05", "10095", "10096", "10078", "10078.5", 1000),  # close_r ~ -0.85R, low above hard stop
    ]

    trade = simulate_proxy_trade(
        signal,
        rows,
        strategy_name="CPR Trend-Day Debit Spread",
        underlying="NIFTY",
        underlying_symbol="NSE:NIFTY50-INDEX",
        stop_pct=Decimal("0.0025"),
        target_r=Decimal("2"),
        time_exit=time(14, 45),
        cost_rupees=Decimal("120"),
    )

    assert trade is not None
    assert trade.exit_reason == "premium_stop"
    assert Decimal("-0.95") < trade.pnl_r < Decimal("-0.7")


def test_single_stock_signal_inherits_structure_level_from_stock_breakout():
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
    assert signal.metadata.get("structure_level") is not None


def test_load_config_parses_cpr_underlyings_restriction(tmp_path: Path):
    data = config_to_json_dict(build_default_config())
    data["strategies"]["cpr_trend_debit_spread"]["underlyings"] = ["BANKNIFTY"]
    config_path = tmp_path / "pack.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")

    cfg = load_config(config_path)

    assert cfg.strategies["cpr_trend_debit_spread"].underlyings == ("BANKNIFTY",)
    # Default stays both when the key is absent.
    assert cfg.strategies["nifty_orb_debit_spread"].underlyings == ("NIFTY", "BANKNIFTY")
