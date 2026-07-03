from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.banknifty_options_paper as bn
from scripts.banknifty_options_paper import (
    FyersOptionContract,
    build_stop_target,
    cap_stop_by_trade_loss,
    compute_mfe_ratchet_stop,
    compute_profit_lock_stop,
    evaluate_chop_regime,
    evaluate_lunch_chop_guard,
    evaluate_option_exit,
    evaluate_pullback_continuation,
    evaluate_stagnation_exit,
    evaluate_weighted_vwap_side,
    json_dumps_safe,
    nearest_strike,
    parse_fyers_option_row,
    select_atm_contracts,
    select_directional_contract_candidates,
    rank_chain_candidates,
    should_run_entry_scan,
    size_lots_by_risk,
    size_option_lots,
)


SAMPLE_CE_ROW = [
    "101126063035000",
    "BANKNIFTY 30 Jun 26 65400 CE",
    "14",
    "30",
    "0.05",
    "",
    "0915-1530|1815-1915:",
    "2026-06-05",
    "1782813600",
    "NSE:BANKNIFTY26JUN65400CE",
    "10",
    "11",
    "35000",
    "BANKNIFTY",
    "26009",
    "65400.0",
    "CE",
    "101000000026009",
    "None",
    "0",
    "0.0",
]


def test_parse_fyers_option_row_extracts_banknifty_contract_fields() -> None:
    contract = parse_fyers_option_row(SAMPLE_CE_ROW)

    assert contract is not None
    assert contract.symbol == "NSE:BANKNIFTY26JUN65400CE"
    assert contract.underlying == "BANKNIFTY"
    assert contract.expiry == datetime(2026, 6, 30, tzinfo=timezone.utc).date()
    assert contract.strike == Decimal("65400.0")
    assert contract.option_type == "CE"
    assert contract.lot_size == 30
    assert contract.tick_size == Decimal("0.05")


def test_nearest_strike_rounds_to_banknifty_hundred_point_step() -> None:
    assert nearest_strike(Decimal("65447"), Decimal("100")) == Decimal("65400")
    assert nearest_strike(Decimal("65450"), Decimal("100")) == Decimal("65500")


def test_select_atm_contracts_picks_nearest_expiry_and_ce_pe_at_atm() -> None:
    contracts = [
        FyersOptionContract("NSE:BANKNIFTY26JUN65400CE", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65400"), "CE", 30, Decimal("0.05"), {}),
        FyersOptionContract("NSE:BANKNIFTY26JUN65400PE", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65400"), "PE", 30, Decimal("0.05"), {}),
        FyersOptionContract("NSE:BANKNIFTY26JUL65400CE", "BANKNIFTY", datetime(2026, 7, 28).date(), Decimal("65400"), "CE", 30, Decimal("0.05"), {}),
    ]

    selected = select_atm_contracts(contracts, underlying_ltp=Decimal("65447"), today=datetime(2026, 6, 8).date())

    assert selected["CE"].symbol == "NSE:BANKNIFTY26JUN65400CE"
    assert selected["PE"].symbol == "NSE:BANKNIFTY26JUN65400PE"


def test_select_directional_contract_candidates_returns_atm_then_one_otm_only() -> None:
    contracts = [
        FyersOptionContract("CE65300", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65300"), "CE", 30, Decimal("0.05"), {}),
        FyersOptionContract("CE65400", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65400"), "CE", 30, Decimal("0.05"), {}),
        FyersOptionContract("CE65500", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65500"), "CE", 30, Decimal("0.05"), {}),
        FyersOptionContract("CE65600", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65600"), "CE", 30, Decimal("0.05"), {}),
        FyersOptionContract("PE65300", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65300"), "PE", 30, Decimal("0.05"), {}),
        FyersOptionContract("PE65400", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65400"), "PE", 30, Decimal("0.05"), {}),
    ]

    ce = select_directional_contract_candidates(contracts, direction="CE", underlying_ltp=Decimal("65447"), today=datetime(2026, 6, 8).date(), strike_step=Decimal("100"))
    pe = select_directional_contract_candidates(contracts, direction="PE", underlying_ltp=Decimal("65447"), today=datetime(2026, 6, 8).date(), strike_step=Decimal("100"))

    assert [c.symbol for c in ce] == ["CE65400", "CE65500"]
    assert [c.symbol for c in pe] == ["PE65400", "PE65300"]


def _chain_candidates() -> list:
    return [
        FyersOptionContract("CE65400", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65400"), "CE", 30, Decimal("0.05"), {}),
        FyersOptionContract("CE65500", "BANKNIFTY", datetime(2026, 6, 30).date(), Decimal("65500"), "CE", 30, Decimal("0.05"), {}),
    ]


def test_rank_chain_candidates_promotes_higher_oi_then_tighter_spread() -> None:
    candidates = _chain_candidates()  # ATM CE65400 first by default
    metrics = {
        "CE65400": {"oi": 40000, "bid": 100, "ask": 104},
        "CE65500": {"oi": 90000, "bid": 80, "ask": 81},  # more liquid OTM
    }
    ranked = rank_chain_candidates(candidates, metrics)
    assert [c.symbol for c in ranked] == ["CE65500", "CE65400"]


def test_rank_chain_candidates_breaks_oi_ties_on_spread() -> None:
    candidates = _chain_candidates()
    metrics = {
        "CE65400": {"oi": 50000, "bid": 100, "ask": 110},  # wide spread
        "CE65500": {"oi": 50000, "bid": 100, "ask": 101},  # tight spread
    }
    ranked = rank_chain_candidates(candidates, metrics)
    assert [c.symbol for c in ranked] == ["CE65500", "CE65400"]


def test_rank_chain_candidates_unchanged_without_metrics() -> None:
    candidates = _chain_candidates()
    assert rank_chain_candidates(candidates, {}) == candidates


def test_rank_chain_candidates_sinks_candidate_missing_metrics() -> None:
    candidates = _chain_candidates()
    metrics = {"CE65500": {"oi": 10000, "bid": 80, "ask": 81}}  # ATM has no chain row
    ranked = rank_chain_candidates(candidates, metrics)
    assert [c.symbol for c in ranked] == ["CE65500", "CE65400"]


def test_size_option_lots_never_exceeds_max_premium_exposure() -> None:
    lots, quantity, premium_value = size_option_lots(Decimal("48"), lot_size=30, max_premium_exposure=Decimal("1500"))

    assert lots == 1
    assert quantity == 30
    assert premium_value == Decimal("1440.00")


def test_size_option_lots_skips_when_one_lot_exceeds_exposure() -> None:
    lots, quantity, premium_value = size_option_lots(Decimal("60"), lot_size=30, max_premium_exposure=Decimal("1500"))

    assert lots == 0
    assert quantity == 0
    assert premium_value == Decimal("0.00")


def test_size_lots_by_risk_reduces_by_risk_and_exposure_caps() -> None:
    lots, quantity, exposure, risk = size_lots_by_risk(
        entry_premium=Decimal("100"),
        estimated_stop_premium=Decimal("80"),
        lot_size=30,
        max_trade_loss=Decimal("1500"),
        max_premium_exposure=Decimal("40000"),
    )

    assert lots == 2
    assert quantity == 60
    assert exposure == Decimal("6000.00")
    assert risk == Decimal("1200.00")


def test_size_lots_by_risk_skips_when_one_lot_exceeds_trade_loss() -> None:
    lots, quantity, exposure, risk = size_lots_by_risk(
        entry_premium=Decimal("100"),
        estimated_stop_premium=Decimal("40"),
        lot_size=30,
        max_trade_loss=Decimal("1500"),
        max_premium_exposure=Decimal("40000"),
    )

    assert (lots, quantity, exposure, risk) == (0, 0, Decimal("0.00"), Decimal("0.00"))


def test_build_stop_target_uses_30pct_stop_and_50pct_target_defaults() -> None:
    stop, target = build_stop_target(Decimal("100"), stop_loss_pct=Decimal("0.30"), target_pct=Decimal("0.50"), tick_size=Decimal("0.05"))

    assert stop == Decimal("70.00")
    assert target == Decimal("150.00")


def test_trade_loss_cap_tightens_initial_stop_for_large_banknifty_premium() -> None:
    stop = cap_stop_by_trade_loss(Decimal("1237.95"), Decimal("866.55"), 30, Decimal("3000"), Decimal("0.05"))

    assert stop == Decimal("1137.95")


def test_profit_lock_stop_steps_up_after_best_observed_pnl_crosses_trigger() -> None:
    stop = compute_profit_lock_stop(
        Decimal("1237.95"),
        Decimal("1360.55"),
        30,
        profit_lock_trigger=Decimal("1000"),
        profit_lock_step=Decimal("500"),
        tick_size=Decimal("0.05"),
    )

    assert stop == Decimal("1337.95")


def test_profit_lock_activation_locks_one_step_at_exact_trigger() -> None:
    stop = compute_profit_lock_stop(
        Decimal("100"),
        Decimal("150"),
        30,
        profit_lock_trigger=Decimal("1500"),
        profit_lock_step=Decimal("500"),
        tick_size=Decimal("0.05"),
    )

    assert stop == Decimal("116.65")


def test_evaluate_option_exit_can_disable_fixed_target_for_trailing_runner() -> None:
    now = datetime(2026, 6, 8, 9, 50, tzinfo=timezone.utc)
    entry_time = now - timedelta(minutes=10)

    assert evaluate_option_exit(
        Decimal("160"),
        Decimal("100"),
        Decimal("70"),
        Decimal("150"),
        30,
        now=now,
        entry_time=entry_time,
        force_exit_utc=None,
        target_exit_enabled=False,
    ) == (None, None, None)


def test_mfe_ratchet_locks_about_65pct_of_mfe_after_one_r() -> None:
    # MFE ₹1,819 on 30 qty, R ₹1,500 -> lock 1819 - max(600, 35%*1819) = ₹1,182.35
    stop = compute_mfe_ratchet_stop(
        Decimal("995"),
        Decimal("1055.65"),
        30,
        risk_rupees=Decimal("1500"),
        breakeven_at_r=Decimal("0.8"),
        ratchet_start_r=Decimal("1.0"),
        ratchet_giveback_pct=Decimal("35"),
        ratchet_giveback_min_inr=Decimal("600"),
        tick_size=Decimal("0.05"),
    )

    assert stop == Decimal("1034.40")


def test_mfe_ratchet_moves_to_breakeven_at_point_eight_r() -> None:
    stop = compute_mfe_ratchet_stop(
        Decimal("100"),
        Decimal("140"),
        30,
        risk_rupees=Decimal("1500"),
        breakeven_at_r=Decimal("0.8"),
        ratchet_start_r=Decimal("1.0"),
        ratchet_giveback_pct=Decimal("35"),
        ratchet_giveback_min_inr=Decimal("600"),
        tick_size=Decimal("0.05"),
    )

    assert stop == Decimal("100.05")


def test_stagnation_exit_after_30m_when_momentum_gone_and_pnl_below_point_three_r() -> None:
    now = datetime(2026, 6, 8, 10, 25, tzinfo=timezone.utc)
    entry_time = now - timedelta(minutes=31)

    assert evaluate_stagnation_exit(
        pnl=Decimal("200"),
        risk_rupees=Decimal("1500"),
        now=now,
        entry_time=entry_time,
        stagnation_minutes=30,
        stagnation_min_r=Decimal("0.3"),
        momentum_gone=True,
    ) == "stagnation_exit"


def test_evaluate_option_exit_closes_on_profit_lock_stop() -> None:
    now = datetime(2026, 6, 8, 9, 50, tzinfo=timezone.utc)
    entry_time = now - timedelta(minutes=10)

    assert evaluate_option_exit(
        Decimal("1119.20"),
        Decimal("1237.95"),
        Decimal("1137.95"),
        Decimal("1856.95"),
        30,
        now=now,
        entry_time=entry_time,
        force_exit_utc=None,
        highest_premium=Decimal("1360.55"),
        profit_lock_trigger=Decimal("1000"),
        profit_lock_step=Decimal("500"),
        tick_size=Decimal("0.05"),
        # LTP collapsed past the raised stop between polls; the fill is the
        # observable LTP, not the stop level the market never traded at.
    ) == ("profit_lock_stop", Decimal("1119.20"), Decimal("-3562.50"))


def test_evaluate_option_exit_closes_on_stop_target_and_intraday_force_exit() -> None:
    now = datetime(2026, 6, 8, 9, 50, tzinfo=timezone.utc)
    entry_time = now - timedelta(minutes=10)

    # A gap below the stop fills at the LTP (69), not the untraded stop level (70).
    assert evaluate_option_exit(Decimal("69"), Decimal("100"), Decimal("70"), Decimal("150"), 30, now=now, entry_time=entry_time, force_exit_utc=None) == ("stop_loss", Decimal("69"), Decimal("-930.00"))
    assert evaluate_option_exit(Decimal("151"), Decimal("100"), Decimal("70"), Decimal("150"), 30, now=now, entry_time=entry_time, force_exit_utc=None) == ("target", Decimal("150"), Decimal("1500.00"))
    assert evaluate_option_exit(Decimal("110"), Decimal("100"), Decimal("70"), Decimal("150"), 30, now=now, entry_time=entry_time, force_exit_utc=now - timedelta(seconds=1)) == ("force_intraday_exit", Decimal("110"), Decimal("300.00"))


def test_stale_quote_force_exit_closes_at_entry_without_waiting_for_fresh_quote() -> None:
    now = datetime(2026, 6, 8, 9, 50, tzinfo=timezone.utc)

    assert bn.evaluate_stale_quote_force_exit(
        entry_premium=Decimal("100"),
        quantity=30,
        now=now,
        force_exit_utc=now - timedelta(seconds=1),
    ) == ("force_intraday_exit_stale_quote", Decimal("100"), Decimal("0.00"))


def test_stale_quote_before_force_exit_does_not_close() -> None:
    now = datetime(2026, 6, 8, 9, 50, tzinfo=timezone.utc)

    assert bn.evaluate_stale_quote_force_exit(
        entry_premium=Decimal("100"),
        quantity=30,
        now=now,
        force_exit_utc=now + timedelta(minutes=1),
    ) == (None, None, None)


def test_pre_entry_scan_runs_only_on_five_minute_boundaries() -> None:
    assert should_run_entry_scan(datetime(2026, 6, 8, 9, 20, tzinfo=timezone.utc), 5)
    assert not should_run_entry_scan(datetime(2026, 6, 8, 9, 22, tzinfo=timezone.utc), 5)
    assert should_run_entry_scan(datetime(2026, 6, 8, 9, 22, tzinfo=timezone.utc), 1)


def test_weighted_vwap_side_requires_60pct_on_directional_side() -> None:
    moves = [
        bn.ConstituentMove("A", "A", Decimal("110"), Decimal("100"), Decimal("1"), Decimal("0.40"), Decimal("0.4"), vwap=Decimal("108"), relative_volume=Decimal("1.5")),
        bn.ConstituentMove("B", "B", Decimal("90"), Decimal("100"), Decimal("-1"), Decimal("0.25"), Decimal("-0.25"), vwap=Decimal("92"), relative_volume=Decimal("1.5")),
        bn.ConstituentMove("C", "C", Decimal("105"), Decimal("100"), Decimal("0.5"), Decimal("0.35"), Decimal("0.175"), vwap=Decimal("104"), relative_volume=Decimal("1.5")),
    ]

    decision = evaluate_weighted_vwap_side(moves, direction="CE", min_side_pct=Decimal("60"))

    assert decision.allowed
    assert decision.raw["weighted_vwap_side_pct"] == "75.00"


def test_pullback_continuation_enters_after_retest_holds_broken_level() -> None:
    candles = [
        {"open": Decimal("100"), "high": Decimal("101"), "low": Decimal("99"), "close": Decimal("100")},
        {"open": Decimal("100"), "high": Decimal("102"), "low": Decimal("99.5"), "close": Decimal("101")},
        {"open": Decimal("101"), "high": Decimal("104"), "low": Decimal("100.5"), "close": Decimal("103.5")},  # leg breaks 102
        {"open": Decimal("103.5"), "high": Decimal("103.7"), "low": Decimal("102.1"), "close": Decimal("102.5")},  # pullback holds
        {"open": Decimal("102.5"), "high": Decimal("104.2"), "low": Decimal("102.4"), "close": Decimal("104.0")},  # closes above prior high
    ]

    signal = evaluate_pullback_continuation(
        direction="CE",
        candles=candles,
        confluence_levels=[Decimal("102")],
        breakout_buffer_pct=Decimal("0.02"),
        level_hold_buffer_pct=Decimal("0.02"),
        structure_stop_buffer_pct=Decimal("0.03"),
        leg_lookback_candles=6,
        max_pullback_candles=4,
    )

    assert signal.confirmed
    assert signal.stop_level == Decimal("102.07")
    assert signal.reference_level == Decimal("102")


def test_lunch_guard_blocks_when_range_and_relvol_not_enough() -> None:
    decision = evaluate_lunch_chop_guard(
        datetime(2026, 6, 8, 12, 30, tzinfo=bn.IST),
        day_high=Decimal("55300"),
        day_low=Decimal("55150"),
        adr10=Decimal("400"),
        index_rel_volume=Decimal("1.10"),
        window_start=bn.parse_time("11:30"),
        window_end=bn.parse_time("13:15"),
        min_range_vs_adr=Decimal("0.6"),
        min_relvol=Decimal("1.3"),
    )

    assert not decision.allowed
    assert "blocked" in decision.reasons[0]


def test_chop_guard_blocks_flat_vwap_proxy_crossing_regime() -> None:
    candles = [
        {"open": Decimal("100"), "high": Decimal("100.10"), "low": Decimal("99.90"), "close": Decimal(str(close)), "volume": 1000}
        for close in [100, 100.08, 99.98, 100.07, 99.97, 100.06, 99.96, 100.05, 99.95, 100.04, 99.94, 100.03]
    ]

    decision = evaluate_chop_regime(
        candles,
        lookback_candles=12,
        max_net_move_pct=Decimal("0.15"),
        max_vwap_crosses=3,
    )

    assert not decision.allowed
    assert decision.raw["vwap_proxy_crosses"] >= 3



def test_json_dumps_safe_handles_decimal_and_datetimes_from_quote_metadata() -> None:
    payload = {
        "ltp": Decimal("123.45"),
        "quote_time": datetime(2026, 6, 8, 9, 20, tzinfo=timezone.utc),
    }

    assert json_dumps_safe(payload) == '{"ltp": "123.45", "quote_time": "2026-06-08T09:20:00+00:00"}'


def test_tick_does_not_scan_or_monitor_before_entry_on_non_boundary(monkeypatch) -> None:
    config = SimpleNamespace(entry_scan_interval_minutes=5, poll_interval_seconds=15)
    calls = {"scan": 0, "monitor": 0}
    monkeypatch.setattr(bn, "now_ist", lambda: datetime(2026, 6, 8, 9, 22, tzinfo=bn.IST))
    monkeypatch.setattr(bn, "has_open_option_trade", lambda _config: False)
    monkeypatch.setattr(bn, "scan_for_entry", lambda *args, **kwargs: calls.__setitem__("scan", calls["scan"] + 1) or [])
    monkeypatch.setattr(bn, "monitor_open_options", lambda *args, **kwargs: calls.__setitem__("monitor", calls["monitor"] + 1) or [])

    assert bn.tick(config, loop_seconds=0) == []
    assert calls == {"scan": 0, "monitor": 0}


def test_tick_scans_every_five_minutes_but_defers_fast_monitor_until_entry(monkeypatch) -> None:
    config = SimpleNamespace(entry_scan_interval_minutes=5, poll_interval_seconds=15)
    calls = {"scan": 0, "monitor": 0}
    open_states = iter([False, False])
    monkeypatch.setattr(bn, "now_ist", lambda: datetime(2026, 6, 8, 9, 25, tzinfo=bn.IST))
    monkeypatch.setattr(bn, "has_open_option_trade", lambda _config: next(open_states))
    monkeypatch.setattr(bn, "scan_for_entry", lambda *args, **kwargs: calls.__setitem__("scan", calls["scan"] + 1) or [])
    monkeypatch.setattr(bn, "monitor_open_options", lambda *args, **kwargs: calls.__setitem__("monitor", calls["monitor"] + 1) or [])

    assert bn.tick(config, loop_seconds=0) == []
    assert calls == {"scan": 1, "monitor": 0}


def test_tick_starts_fast_monitor_after_entry_exists(monkeypatch) -> None:
    config = SimpleNamespace(entry_scan_interval_minutes=5, poll_interval_seconds=15)
    calls = {"scan": 0, "monitor": 0}
    open_states = iter([False, True])
    monkeypatch.setattr(bn, "now_ist", lambda: datetime(2026, 6, 8, 9, 25, tzinfo=bn.IST))
    monkeypatch.setattr(bn, "has_open_option_trade", lambda _config: next(open_states))
    monkeypatch.setattr(bn, "scan_for_entry", lambda *args, **kwargs: calls.__setitem__("scan", calls["scan"] + 1) or ["opened"])
    monkeypatch.setattr(bn, "monitor_open_options", lambda *args, **kwargs: calls.__setitem__("monitor", calls["monitor"] + 1) or [])

    assert bn.tick(config, loop_seconds=0) == ["opened"]
    assert calls == {"scan": 1, "monitor": 1}


def test_tick_emits_open_position_status_every_five_minutes(monkeypatch) -> None:
    config = SimpleNamespace(
        entry_scan_interval_minutes=5,
        open_position_update_interval_minutes=5,
        poll_interval_seconds=15,
    )
    monitor_quiet_values: list[bool] = []
    monkeypatch.setattr(bn, "now_ist", lambda: datetime(2026, 6, 8, 9, 25, tzinfo=bn.IST))
    monkeypatch.setattr(bn, "has_open_option_trade", lambda _config: True)

    def fake_monitor(*args, **kwargs):
        quiet = kwargs["quiet_no_change"]
        monitor_quiet_values.append(quiet)
        return ["open position status"] if not quiet else []

    monkeypatch.setattr(bn, "monitor_open_options", fake_monitor)

    assert bn.tick(config, loop_seconds=0) == ["open position status"]
    assert monitor_quiet_values == [False]


def test_tick_keeps_open_position_status_quiet_between_five_minute_updates(monkeypatch) -> None:
    config = SimpleNamespace(
        entry_scan_interval_minutes=5,
        open_position_update_interval_minutes=5,
        poll_interval_seconds=15,
    )
    monitor_quiet_values: list[bool] = []
    monkeypatch.setattr(bn, "now_ist", lambda: datetime(2026, 6, 8, 9, 26, tzinfo=bn.IST))
    monkeypatch.setattr(bn, "has_open_option_trade", lambda _config: True)

    def fake_monitor(*args, **kwargs):
        monitor_quiet_values.append(kwargs["quiet_no_change"])
        return []

    monkeypatch.setattr(bn, "monitor_open_options", fake_monitor)

    assert bn.tick(config, loop_seconds=0) == []
    assert monitor_quiet_values == [True]


def test_strategy_router_parses_only_enabled_paper_safe_entry_cards() -> None:
    cards = bn.parse_strategy_router(
        [
            {
                "id": "banknifty_constituent_led_directional_long_options",
                "name": "BankNifty Constituent-Led Directional Long Options",
                "enabled": True,
                "paper_trade_enabled": True,
                "entry_function": "constituent_led_long_options",
                "card_type": "entry",
            },
            {
                "id": "options_360_short_straddle_strangle_premium_decay",
                "name": "Options 360 Short Straddle/Strangle Premium Decay",
                "enabled": True,
                "paper_trade_enabled": False,
                "entry_function": "research_only",
            },
            {
                "id": "options_greeks_risk_filter",
                "name": "Options Greeks Risk Filter",
                "enabled": False,
                "paper_trade_enabled": False,
                "entry_function": "risk_filter",
            },
        ]
    )

    runnable = bn.runnable_entry_strategy_cards(cards)

    assert [card.strategy_id for card in runnable] == ["banknifty_constituent_led_directional_long_options"]


def test_load_config_refuses_nested_risk_block_that_disagrees_with_top_level(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps(
            {
                "campaign_name": "risk_drift_test",
                "paper_only": True,
                "live_orders_enabled": False,
                "max_trade_loss": 1500,
                "max_daily_loss": 5000,
                "max_trades_per_day": 3,
                "risk": {"max_trade_loss_inr": 999},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        bn.load_config(config_path)


def test_load_config_accepts_nested_risk_block_matching_top_level(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps(
            {
                "campaign_name": "risk_match_test",
                "paper_only": True,
                "live_orders_enabled": False,
                "max_trade_loss": 1500,
                "max_daily_loss": 5000,
                "max_trades_per_day": 3,
                "max_open_positions": 1,
                "max_premium_exposure": 40000,
                "risk": {"max_trade_loss_inr": 1500, "max_daily_loss_inr": 5000, "max_trades_per_day": 3, "max_open_positions": 1, "max_premium_exposure_inr": 40000},
            }
        ),
        encoding="utf-8",
    )

    config = bn.load_config(config_path)
    assert config.max_trade_loss == Decimal("1500")


def test_load_config_attaches_strategy_router_from_json(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps(
            {
                "campaign_name": "router_test",
                "paper_only": True,
                "live_orders_enabled": False,
                "strategy_router": [
                    {
                        "id": "banknifty_constituent_led_directional_long_options",
                        "name": "BankNifty Constituent-Led Directional Long Options",
                        "enabled": True,
                        "paper_trade_enabled": True,
                        "entry_function": "constituent_led_long_options",
                        "card_type": "entry",
                    },
                    {
                        "id": "breakout_continuation",
                        "name": "Breakout Continuation",
                        "enabled": False,
                        "paper_trade_enabled": False,
                        "entry_function": "not_implemented",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    config = bn.load_config(config_path)

    assert [card.strategy_id for card in config.strategy_router] == [
        "banknifty_constituent_led_directional_long_options",
        "breakout_continuation",
    ]
    assert [card.strategy_id for card in bn.runnable_entry_strategy_cards(config.strategy_router)] == [
        "banknifty_constituent_led_directional_long_options"
    ]


def test_parse_option_quote_metrics_reads_nested_fyers_raw_v_shape() -> None:
    meta = {
        "volume": None,
        "raw": {
            "v": {
                "bid": 99.0,
                "ask": 101.0,
                "spread": 2.0,
                "volume": 5000,
                "delta": 0.55,
                "theta": -8.2,
                "iv": 18.5,
            }
        },
    }

    metrics = bn.parse_option_quote_metrics(meta, ltp=Decimal("100"))

    assert metrics.bid == Decimal("99.0")
    assert metrics.ask == Decimal("101.0")
    assert metrics.spread == Decimal("2.0")
    assert metrics.spread_pct == Decimal("2.00")
    assert metrics.volume == 5000
    assert metrics.delta == Decimal("0.55")
    assert metrics.theta == Decimal("-8.2")
    assert metrics.iv == Decimal("18.5")


def test_parse_option_quote_metrics_reads_flat_shape_and_greek_aliases() -> None:
    meta = {
        "bid": 99,
        "ask": 101,
        "volume": 1200,
        "delta": -0.4,
        "implied_volatility": 22.0,
    }

    metrics = bn.parse_option_quote_metrics(meta, ltp=Decimal("100"))

    assert metrics.spread == Decimal("2")  # computed from ask - bid when no explicit spread
    assert metrics.volume == 1200
    assert metrics.delta == Decimal("-0.4")
    assert metrics.iv == Decimal("22.0")


def test_parse_risk_filter_config_uses_safe_paper_defaults() -> None:
    rf = bn.parse_risk_filter_config(None)

    assert rf.enabled is False
    assert rf.enforce_spread_filter is True
    assert rf.max_spread_pct == Decimal("3.0")
    assert rf.max_spread_rupees == Decimal("5.0")
    assert rf.min_volume == 0
    assert rf.min_oi == 0
    assert rf.require_greeks is False
    assert rf.min_abs_delta == Decimal("0.25")
    assert rf.max_abs_theta == Decimal("0")
    assert rf.max_iv == Decimal("0")
    assert rf.require_iv is False


def test_evaluate_option_risk_filters_allows_tight_spread_with_advisory_missing_greeks_when_delta_floor_disabled() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_abs_delta": 0})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is True
    assert decision.reasons == []
    assert any("delta" in w or "greek" in w for w in decision.warnings)
    assert decision.raw["spread_pct"] == "2.00"


def test_evaluate_option_risk_filters_rejects_missing_spread_when_spread_filter_enabled() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_abs_delta": 0})
    meta = {"raw": {"v": {"volume": 5000}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("spread unavailable" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_wide_spread_pct() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "max_spread_rupees": 0})
    meta = {"raw": {"v": {"bid": 100, "ask": 110}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("105"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("spread" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_wide_spread_rupees() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "max_spread_pct": 0, "max_spread_rupees": 5})
    meta = {"raw": {"v": {"bid": 100, "ask": 110}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("105"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("spread" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_low_volume() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_volume": 1000})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 500}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("volume" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_missing_volume_when_volume_floor_configured() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_volume": 1000, "min_abs_delta": 0})
    meta = {"raw": {"v": {"bid": 99, "ask": 101}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("volume unavailable" in r for r in decision.reasons)


def test_parse_risk_filter_config_reads_min_oi() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_oi": 50000})
    assert rf.min_oi == 50000


def test_option_quote_metrics_parses_open_interest_from_chain_keys() -> None:
    # OI arrives merged into the quote meta from the option-chain snapshot.
    metrics = bn.parse_option_quote_metrics({"bid": 99, "ask": 101, "oi": 75000})
    assert metrics.oi == 75000
    metrics_alt = bn.parse_option_quote_metrics({"open_interest": 12345})
    assert metrics_alt.oi == 12345


def test_evaluate_option_risk_filters_rejects_low_open_interest() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_oi": 50000, "min_abs_delta": 0})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "oi": 1000}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("open interest" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_missing_oi_when_floor_configured() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_oi": 50000, "min_abs_delta": 0})
    meta = {"raw": {"v": {"bid": 99, "ask": 101}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("open interest unavailable" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_allows_when_chain_metrics_satisfy_caps() -> None:
    # With chain-sourced greeks/IV/OI merged in, the previously-dormant caps enforce
    # and a healthy ATM option passes them all.
    rf = bn.parse_risk_filter_config(
        {"enabled": True, "min_oi": 50000, "min_abs_delta": 0.25, "max_iv": 30}
    )
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000, "oi": 120000, "delta": 0.55, "iv": 18.0}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is True
    assert decision.reasons == []
    assert decision.raw["oi"] == 120000


def test_evaluate_option_risk_filters_rejects_when_greeks_required_but_missing() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "require_greeks": True})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("greek" in r or "delta" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_missing_delta_when_delta_floor_configured() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_abs_delta": 0.25})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("delta" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_low_abs_delta() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_abs_delta": 0.25})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000, "delta": 0.10}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("delta" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_high_abs_theta_when_capped() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "max_abs_theta": 5})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000, "delta": 0.5, "theta": -8.2}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("theta" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_missing_theta_when_theta_cap_configured() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "min_abs_delta": 0, "max_abs_theta": 5})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("theta" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_high_iv_when_capped() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "max_iv": 20})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000, "delta": 0.5, "iv": 35.0}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("iv" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_rejects_missing_iv_when_iv_cap_configured() -> None:
    rf = bn.parse_risk_filter_config({"enabled": True, "max_iv": 20})
    meta = {"raw": {"v": {"bid": 99, "ask": 101, "volume": 5000, "delta": 0.5}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("100"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is False
    assert any("implied volatility" in r or "iv" in r for r in decision.reasons)


def test_evaluate_option_risk_filters_disabled_allows_everything() -> None:
    rf = bn.parse_risk_filter_config({"enabled": False})
    meta = {"raw": {"v": {"bid": 100, "ask": 200, "volume": 1}}}

    decision = bn.evaluate_option_risk_filters(
        option_ltp=Decimal("150"), option_meta=meta, option_type="CE", risk_filter=rf
    )

    assert decision.allowed is True
    assert decision.reasons == []


def test_load_config_parses_risk_filter_block(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps(
            {
                "campaign_name": "rf_test",
                "paper_only": True,
                "live_orders_enabled": False,
                "risk_filter": {
                    "enabled": True,
                    "max_spread_pct": 2.5,
                    "min_volume": 1000,
                },
            }
        ),
        encoding="utf-8",
    )

    config = bn.load_config(config_path)

    assert config.risk_filter.enabled is True
    assert config.risk_filter.max_spread_pct == Decimal("2.5")
    assert config.risk_filter.min_volume == 1000
    # Unspecified fields fall back to safe defaults.
    assert config.risk_filter.max_spread_rupees == Decimal("5.0")
    assert config.risk_filter.require_greeks is False


def test_load_config_rejects_string_false_paper_only(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps({"campaign_name": "unsafe", "paper_only": "false", "live_orders_enabled": False}),
        encoding="utf-8",
    )

    try:
        bn.load_config(config_path)
    except SystemExit as exc:
        assert "paper_only" in str(exc)
    else:
        raise AssertionError("load_config should reject string false paper_only")


def test_load_config_rejects_unknown_string_paper_only(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps({"campaign_name": "unsafe", "paper_only": "maybe", "live_orders_enabled": False}),
        encoding="utf-8",
    )

    try:
        bn.load_config(config_path)
    except SystemExit as exc:
        assert "paper_only" in str(exc)
    else:
        raise AssertionError("load_config should reject unknown paper_only string")


def test_load_config_rejects_unknown_string_live_orders_enabled(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps({"campaign_name": "unsafe", "paper_only": True, "live_orders_enabled": "maybe"}),
        encoding="utf-8",
    )

    try:
        bn.load_config(config_path)
    except SystemExit as exc:
        assert "live_orders_enabled" in str(exc)
    else:
        raise AssertionError("load_config should reject unknown live_orders_enabled string")


def test_load_config_rejects_daily_loss_cap_that_is_smaller_than_worst_case_trade_losses(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps(
            {
                "campaign_name": "unsafe_daily_cap",
                "paper_only": True,
                "live_orders_enabled": False,
                "max_daily_loss": 5000,
                "max_trades_per_day": 4,
                "max_trade_loss": 1500,
            }
        ),
        encoding="utf-8",
    )

    try:
        bn.load_config(config_path)
    except SystemExit as exc:
        assert "max_trades_per_day * max_trade_loss" in str(exc)
    else:
        raise AssertionError("load_config should reject daily cap breach math")


def test_live_config_active_entry_strategy_declares_card_type_entry() -> None:
    config = bn.load_config()
    active = [card for card in config.strategy_router if card.strategy_id == "banknifty_constituent_led_directional_long_options"]

    assert len(active) == 1
    assert active[0].card_type == "entry"
    assert [card.strategy_id for card in bn.runnable_entry_strategy_cards(config.strategy_router)] == [
        "banknifty_constituent_led_directional_long_options"
    ]


def test_filter_cards_are_never_runnable_entry_strategies() -> None:
    cards = bn.parse_strategy_router(
        [
            {
                "id": "banknifty_constituent_led_directional_long_options",
                "name": "BankNifty Constituent-Led Directional Long Options",
                "enabled": True,
                "paper_trade_enabled": True,
                "entry_function": "constituent_led_long_options",
                "card_type": "entry",
            },
            {
                "id": "options_greeks_risk_filter",
                "name": "Options Greeks Risk Filter",
                "enabled": True,
                "paper_trade_enabled": True,
                "entry_function": "options_greeks_risk_filter",
                "card_type": "filter",
            },
        ]
    )

    runnable = bn.runnable_entry_strategy_cards(cards)

    assert [card.strategy_id for card in runnable] == [
        "banknifty_constituent_led_directional_long_options"
    ]
    assert [card.strategy_id for card in bn.filter_cards(cards)] == ["options_greeks_risk_filter"]


def test_direct_strategy_card_construction_without_card_type_is_not_runnable() -> None:
    card = bn.StrategyCardRule(
        strategy_id="direct_missing_type",
        name="Direct Missing Type",
        enabled=True,
        paper_trade_enabled=True,
        entry_function="constituent_led_long_options",
    )

    assert card.card_type == "unspecified"
    assert bn.runnable_entry_strategy_cards((card,)) == ()


def test_missing_card_type_is_not_runnable_by_default() -> None:
    cards = bn.parse_strategy_router(
        [
            {
                "id": "missing_type_strategy",
                "enabled": True,
                "paper_trade_enabled": True,
                "entry_function": "constituent_led_long_options",
            }
        ]
    )

    assert cards[0].card_type == "unspecified"
    assert bn.runnable_entry_strategy_cards(cards) == ()


def test_guardrail_cards_are_never_runnable_entry_strategies() -> None:
    cards = bn.parse_strategy_router(
        [
            {
                "id": "banknifty_constituent_led_directional_long_options",
                "name": "BankNifty Constituent-Led Directional Long Options",
                "enabled": True,
                "paper_trade_enabled": True,
                "entry_function": "constituent_led_long_options",
                "card_type": "entry",
            },
            {
                "id": "trading_psychology_execution_guardrails",
                "name": "Trading Psychology Execution Guardrails",
                "enabled": True,
                "paper_trade_enabled": True,
                "entry_function": "guardrail_partially_implemented",
                "card_type": "guardrail",
            },
        ]
    )

    runnable = bn.runnable_entry_strategy_cards(cards)

    assert [card.strategy_id for card in runnable] == [
        "banknifty_constituent_led_directional_long_options"
    ]
    assert [card.strategy_id for card in bn.guardrail_cards(cards)] == [
        "trading_psychology_execution_guardrails"
    ]


def test_parse_strategy_router_reads_card_type_aliases() -> None:
    cards = bn.parse_strategy_router(
        [
            {"id": "a", "kind": "filter", "enabled": True, "paper_trade_enabled": True, "entry_function": "x"},
            {"id": "b", "role": "guardrail", "enabled": True, "paper_trade_enabled": True, "entry_function": "y"},
        ]
    )

    assert cards[0].card_type == "filter"
    assert cards[1].card_type == "guardrail"
    assert bn.runnable_entry_strategy_cards(cards) == ()


def test_scan_for_entry_refuses_when_no_router_strategy_enabled(monkeypatch) -> None:
    config = SimpleNamespace(
        strategy_router=(
            bn.StrategyCardRule(
                strategy_id="banknifty_constituent_led_directional_long_options",
                name="BankNifty Constituent-Led Directional Long Options",
                enabled=False,
                paper_trade_enabled=True,
                entry_function="constituent_led_long_options",
                status="paper_ready",
                source="",
                notes="",
            ),
        )
    )
    monkeypatch.setattr(bn, "connect_db", lambda: (_ for _ in ()).throw(AssertionError("DB should not be touched")))

    lines = bn.scan_for_entry(config, quiet_no_change=False)

    assert lines[-1] == "No trade: no enabled paper-safe entry strategy is runnable in the strategy router."



def test_major_constituent_jump_reason_flags_large_vwap_confirmed_high_volume_move() -> None:
    move = bn.ConstituentMove(
        symbol="HDFCBANK",
        fyers_symbol="NSE:HDFCBANK-EQ",
        ltp=Decimal("754.55"),
        open=Decimal("736.50"),
        pct_from_open=Decimal("2.45"),
        normalized_weight=Decimal("0.1793"),
        contribution=Decimal("0.4393"),
        vwap=Decimal("743.05"),
        volume=30325134,
        relative_volume=Decimal("1.75"),
    )

    reason = bn.major_constituent_jump_reason(
        move,
        direction="CE",
        major_jump_threshold_pct=Decimal("1.50"),
        rel_volume_threshold=Decimal("1.50"),
    )

    assert reason is not None
    assert reason.symbol == "HDFCBANK"
    assert reason.vwap_confirmed is True
    assert reason.relative_volume_confirmed is True
    assert "major upside jump" in reason.summary
    assert "news/reason review" in reason.summary


def test_vwap_volume_confirmation_requires_top_weighted_movers_to_confirm() -> None:
    confirmed = bn.ConstituentMove(
        "HDFCBANK",
        "NSE:HDFCBANK-EQ",
        Decimal("110"),
        Decimal("100"),
        Decimal("10"),
        Decimal("0.60"),
        Decimal("6"),
        vwap=Decimal("105"),
        relative_volume=Decimal("2.0"),
    )
    weak = bn.ConstituentMove(
        "ICICIBANK",
        "NSE:ICICIBANK-EQ",
        Decimal("101"),
        Decimal("100"),
        Decimal("1"),
        Decimal("0.40"),
        Decimal("0.4"),
        vwap=Decimal("102"),
        relative_volume=Decimal("0.8"),
    )

    decision = bn.evaluate_vwap_volume_confirmation(
        [confirmed, weak],
        direction="CE",
        min_confirming_top_movers=1,
        rel_volume_threshold=Decimal("1.50"),
    )

    assert decision.allowed is True
    assert decision.confirmed_symbols == ["HDFCBANK"]
    assert any("VWAP" in reason for reason in decision.reasons)


def test_index_structure_requires_bullish_breakout_and_sets_trough_stop() -> None:
    candles = [
        {"high": Decimal("100"), "low": Decimal("94"), "close": Decimal("98")},
        {"high": Decimal("103"), "low": Decimal("96"), "close": Decimal("101")},
        {"high": Decimal("105"), "low": Decimal("99"), "close": Decimal("106")},
    ]

    signal = bn.evaluate_index_structure_signal(
        direction="CE",
        current_ltp=Decimal("106"),
        quote_meta={"open": Decimal("98")},
        candles=candles,
        breakout_buffer_pct=Decimal("0"),
        stop_buffer_pct=Decimal("0"),
    )

    assert signal.confirmed is True
    assert signal.stop_level == Decimal("99.00")
    assert "broke above prior swing high" in signal.reason


def test_index_structure_rejects_bullish_signal_without_breakout() -> None:
    candles = [
        {"high": Decimal("100"), "low": Decimal("94"), "close": Decimal("98")},
        {"high": Decimal("105"), "low": Decimal("96"), "close": Decimal("101")},
        {"high": Decimal("104"), "low": Decimal("99"), "close": Decimal("103")},
    ]

    signal = bn.evaluate_index_structure_signal(
        direction="CE",
        current_ltp=Decimal("103"),
        quote_meta={"open": Decimal("98")},
        candles=candles,
        breakout_buffer_pct=Decimal("0"),
        stop_buffer_pct=Decimal("0"),
    )

    assert signal.confirmed is False
    assert "has not broken prior swing high" in signal.reason


def test_index_structure_exit_closes_ce_when_index_breaks_structure_stop() -> None:
    reason, exit_premium, pnl = bn.evaluate_index_structure_exit(
        option_type="CE",
        index_ltp=Decimal("98.90"),
        structure_stop=Decimal("99.00"),
        option_ltp=Decimal("940"),
        entry_premium=Decimal("959.35"),
        quantity=30,
    )

    assert reason == "index_structure_stop"
    assert exit_premium == Decimal("940")
    assert pnl == Decimal("-580.50")


def test_swing_trailing_stop_raises_ce_stop_to_latest_higher_low() -> None:
    candles = [
        {"high": Decimal("100"), "low": Decimal("95"), "close": Decimal("98")},
        {"high": Decimal("108"), "low": Decimal("101"), "close": Decimal("106")},
        {"high": Decimal("110"), "low": Decimal("103"), "close": Decimal("109")},
    ]

    assert bn.compute_swing_trailing_stop(
        direction="CE",
        current_stop=Decimal("99"),
        candles=candles,
        stop_buffer_pct=Decimal("0"),
    ) == Decimal("103.00")



def test_realistic_risk_plan_maps_index_structure_to_option_premium_stop_and_r_target() -> None:
    index_candles = [
        {"high": Decimal("55510.40"), "low": Decimal("55440.00"), "close": Decimal("55480.00")},
        {"high": Decimal("55495.00"), "low": Decimal("55420.00"), "close": Decimal("55440.00")},
        {"high": Decimal("55470.00"), "low": Decimal("55388.90"), "close": Decimal("55410.00")},
        {"high": Decimal("55457.10"), "low": Decimal("55389.25"), "close": Decimal("55423.80")},
    ]
    option_candles = [
        {"high": Decimal("985.60"), "low": Decimal("945.00"), "close": Decimal("960.00")},
        {"high": Decimal("970.00"), "low": Decimal("930.00"), "close": Decimal("948.00")},
        {"high": Decimal("959.45"), "low": Decimal("917.70"), "close": Decimal("940.55")},
    ]

    plan = bn.build_realistic_stop_target(
        entry_premium=Decimal("959.35"),
        option_ltp=Decimal("940.55"),
        index_ltp=Decimal("55423.80"),
        option_type="CE",
        quantity=30,
        max_trade_loss=Decimal("1500"),
        tick_size=Decimal("0.05"),
        option_candles=option_candles,
        index_candles=index_candles,
        observed_option_index_slope=Decimal("0.482"),
        atr_buffer_multiplier=Decimal("0.20"),
        target_r_multiple=Decimal("1.20"),
        max_target_pct=Decimal("0.06"),
    )

    assert plan.index_stop == Decimal("55374.18")
    assert plan.stop_premium == Decimal("916.60")
    assert plan.target_premium == Decimal("1010.65")
    assert plan.risk_rupees == Decimal("1282.50")
    assert plan.target_premium < Decimal("1439.05")
    assert plan.raw["basis"] == "index_structure_mapped_to_option_premium"


def test_realistic_risk_plan_rejects_when_structure_risk_exceeds_trade_cap() -> None:
    plan = bn.build_realistic_stop_target(
        entry_premium=Decimal("1000"),
        option_ltp=Decimal("1000"),
        index_ltp=Decimal("56000"),
        option_type="CE",
        quantity=30,
        max_trade_loss=Decimal("1500"),
        tick_size=Decimal("0.05"),
        option_candles=[{"high": Decimal("1040"), "low": Decimal("850"), "close": Decimal("900")}],
        index_candles=[{"high": Decimal("56100"), "low": Decimal("55000"), "close": Decimal("56000")}],
        observed_option_index_slope=Decimal("0.50"),
        atr_buffer_multiplier=Decimal("0"),
        target_r_multiple=Decimal("1.20"),
        max_target_pct=Decimal("0.06"),
    )

    assert plan is None


def test_load_config_reads_realistic_risk_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "banknifty_options_paper.json"
    config_path.write_text(
        json.dumps(
            {
                "campaign_name": "risk_fields",
                "paper_only": True,
                "live_orders_enabled": False,
                "max_daily_loss": 5000,
                "max_trades_per_day": 3,
                "max_trade_loss": 1500,
                "realistic_risk_enabled": True,
                "structure_candle_resolution": "5",
                "option_structure_lookback_candles": 6,
                "atr_buffer_multiplier": 0.20,
                "target_r_multiple": 1.20,
                "target_pct": 0.06,
                "fixed_target_exit_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    config = bn.load_config(config_path)

    assert config.realistic_risk_enabled is True
    assert config.structure_candle_resolution == "5"
    assert config.option_structure_lookback_candles == 6
    assert config.atr_buffer_multiplier == Decimal("0.2")
    assert config.target_r_multiple == Decimal("1.2")
    assert config.target_pct == Decimal("0.06")
    assert config.fixed_target_exit_enabled is False


def test_parse_chain_signal_config_defaults_are_advisory() -> None:
    cfg = bn.parse_chain_signal_config(None)
    assert cfg.enabled is False
    assert cfg.block_on_iv_regime_high is False
    assert cfg.block_on_contradicting_oi is False
    assert cfg.block_on_pcr_extreme is False
    assert cfg.pcr_bullish_max == Decimal("0")
    assert cfg.pcr_bearish_min == Decimal("0")


def test_parse_chain_signal_config_reads_values() -> None:
    cfg = bn.parse_chain_signal_config(
        {"enabled": True, "block_on_iv_regime_high": True, "pcr_bullish_max": 1.5}
    )
    assert cfg.enabled is True
    assert cfg.block_on_iv_regime_high is True
    assert cfg.pcr_bullish_max == Decimal("1.5")


def test_evaluate_chain_signals_disabled_allows_everything() -> None:
    cfg = bn.parse_chain_signal_config({"enabled": False, "block_on_iv_regime_high": True})
    summary = {"pcr": Decimal("3"), "iv_regime": "high", "oi_buildup_label": "call_buildup"}
    decision = bn.evaluate_chain_signals(direction="CE", summary=summary, cfg=cfg)
    assert decision.allowed is True
    assert decision.reasons == []
    assert decision.warnings == []


def test_evaluate_chain_signals_high_iv_advisory_by_default() -> None:
    cfg = bn.parse_chain_signal_config({"enabled": True})
    summary = {"pcr": None, "iv_regime": "high", "oi_buildup_label": None}
    decision = bn.evaluate_chain_signals(direction="CE", summary=summary, cfg=cfg)
    assert decision.allowed is True
    assert any("IV regime high" in w for w in decision.warnings)


def test_evaluate_chain_signals_high_iv_blocks_when_configured() -> None:
    cfg = bn.parse_chain_signal_config({"enabled": True, "block_on_iv_regime_high": True})
    summary = {"pcr": None, "iv_regime": "high", "oi_buildup_label": None}
    decision = bn.evaluate_chain_signals(direction="PE", summary=summary, cfg=cfg)
    assert decision.allowed is False
    assert any("IV regime high" in r for r in decision.reasons)


def test_evaluate_chain_signals_contradicting_oi_blocks_when_configured() -> None:
    cfg = bn.parse_chain_signal_config({"enabled": True, "block_on_contradicting_oi": True})
    ce = bn.evaluate_chain_signals(
        direction="CE",
        summary={"pcr": None, "iv_regime": "normal", "oi_buildup_label": "call_buildup"},
        cfg=cfg,
    )
    assert ce.allowed is False
    assert any("call OI buildup" in r for r in ce.reasons)
    # put_buildup does not contradict a long CE
    ce_ok = bn.evaluate_chain_signals(
        direction="CE",
        summary={"pcr": None, "iv_regime": "normal", "oi_buildup_label": "put_buildup"},
        cfg=cfg,
    )
    assert ce_ok.allowed is True


def test_evaluate_chain_signals_pcr_extreme_blocks_when_configured() -> None:
    cfg = bn.parse_chain_signal_config(
        {"enabled": True, "block_on_pcr_extreme": True, "pcr_bullish_max": 1.5}
    )
    decision = bn.evaluate_chain_signals(
        direction="CE",
        summary={"pcr": Decimal("2.0"), "iv_regime": "normal", "oi_buildup_label": None},
        cfg=cfg,
    )
    assert decision.allowed is False
    assert any("PCR" in r for r in decision.reasons)


def test_evaluate_chain_signals_aligned_context_passes_clean() -> None:
    cfg = bn.parse_chain_signal_config(
        {"enabled": True, "block_on_iv_regime_high": True, "block_on_contradicting_oi": True}
    )
    summary = {"pcr": Decimal("0.9"), "iv_regime": "low", "oi_buildup_label": "put_buildup"}
    decision = bn.evaluate_chain_signals(direction="CE", summary=summary, cfg=cfg)
    assert decision.allowed is True
    assert decision.reasons == []
    assert decision.warnings == []


# --- control plane: pause + manual force-exit ---------------------------------

def test_partition_force_exit_claims_claims_open_trades_only() -> None:
    rows = [
        (101, {"trade_id": 7}),
        (102, {"trade_id": 99}),       # not open -> rejected
        (103, {"trade_id": 7}),        # duplicate -> rejected
        (104, {"trade_id": "8"}),      # string id of an open trade -> claimed
        (105, {"trade_id": "abc"}),    # unparseable -> rejected
        (106, {}),                     # missing -> rejected
    ]
    claims, rejects = bn.partition_force_exit_claims(rows, {7, 8})
    assert claims == {7: 101, 8: 104}
    assert [request_id for request_id, _ in rejects] == [102, 103, 105, 106]


def test_evaluate_manual_force_exit_uses_ltp_when_available() -> None:
    exit_premium, pnl = bn.evaluate_manual_force_exit(
        entry_premium=Decimal("100"), ltp=Decimal("112.50"), quantity=30
    )
    assert exit_premium == Decimal("112.50")
    assert pnl == Decimal("375.00")


def test_evaluate_manual_force_exit_breakeven_without_quote() -> None:
    exit_premium, pnl = bn.evaluate_manual_force_exit(
        entry_premium=Decimal("100"), ltp=None, quantity=30
    )
    assert exit_premium == Decimal("100")
    assert pnl == Decimal("0.00")


class _FakeCursor:
    def __init__(self, results: list[list[tuple]]) -> None:
        self._results = list(results)
        self._current: list[tuple] = []
        self.statements: list[str] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.statements.append(" ".join(sql.split()))
        self._current = self._results.pop(0) if self._results else []

    def fetchone(self) -> tuple | None:
        return self._current[0] if self._current else None

    def fetchall(self) -> list[tuple]:
        return self._current


def test_control_state_paused_false_when_table_missing() -> None:
    cur = _FakeCursor([[(None,)]])
    assert bn.control_state_paused(cur) is False


def test_control_state_paused_reads_state() -> None:
    cur = _FakeCursor([[("research.control_state",)], [(True,)]])
    assert bn.control_state_paused(cur) is True
    cur = _FakeCursor([[("research.control_state",)], [(False,)]])
    assert bn.control_state_paused(cur) is False
    cur = _FakeCursor([[("research.control_state",)], []])  # no row for engine
    assert bn.control_state_paused(cur) is False


def test_claim_force_exit_requests_skips_when_table_missing() -> None:
    cur = _FakeCursor([[(None,)]])
    assert bn.claim_force_exit_requests(cur, {1, 2}) == {}
