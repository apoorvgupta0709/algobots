from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.apply_control_requests import (
    BANKNIFTY_RISK_MIRROR,
    CONFIG_VALIDATORS,
    ENGINE_BANKNIFTY,
    ENGINE_PACK,
    ENGINE_CONFIG_PATHS,
    RISK_CAP_BOUNDS,
    ControlRequestRejected,
    apply_risk_caps_banknifty,
    apply_risk_caps_pack,
    apply_strategy_toggle_banknifty,
    apply_strategy_toggle_pack,
    validate_cap_changes,
    write_config_atomically,
)


def banknifty_config() -> dict:
    return json.loads(ENGINE_CONFIG_PATHS[ENGINE_BANKNIFTY].read_text(encoding="utf-8"))


def pack_config() -> dict:
    return json.loads(ENGINE_CONFIG_PATHS[ENGINE_PACK].read_text(encoding="utf-8"))


# --- bounds / whitelist -----------------------------------------------------

def test_non_whitelisted_keys_are_rejected() -> None:
    for key in ("paper_only", "live_orders_enabled", "force_exit_time", "starting_capital", "entry_function"):
        with pytest.raises(ControlRequestRejected):
            validate_cap_changes(ENGINE_BANKNIFTY, {key: 1})


def test_out_of_bounds_caps_are_rejected() -> None:
    with pytest.raises(ControlRequestRejected):
        validate_cap_changes(ENGINE_BANKNIFTY, {"max_daily_loss": 100})  # below floor
    with pytest.raises(ControlRequestRejected):
        validate_cap_changes(ENGINE_BANKNIFTY, {"max_daily_loss": 50000})  # above ceiling
    with pytest.raises(ControlRequestRejected):
        validate_cap_changes(ENGINE_BANKNIFTY, {"max_open_positions": 2})  # banknifty is single-position
    with pytest.raises(ControlRequestRejected):
        validate_cap_changes(ENGINE_PACK, {"max_trade_loss": 2000})  # pack validator hard-caps 1500


def test_integer_caps_reject_fractions_and_booleans() -> None:
    with pytest.raises(ControlRequestRejected):
        validate_cap_changes(ENGINE_PACK, {"max_trades_per_day": 2.5})
    with pytest.raises(ControlRequestRejected):
        validate_cap_changes(ENGINE_PACK, {"max_trades_per_day": True})


def test_in_bounds_caps_validate() -> None:
    changes = validate_cap_changes(ENGINE_BANKNIFTY, {"max_daily_loss": 4000, "max_trades_per_day": 2})
    assert changes == {"max_daily_loss": Decimal("4000"), "max_trades_per_day": Decimal("2")}


def test_empty_changes_rejected() -> None:
    with pytest.raises(ControlRequestRejected):
        validate_cap_changes(ENGINE_BANKNIFTY, {})
    with pytest.raises(ControlRequestRejected):
        validate_cap_changes(ENGINE_BANKNIFTY, None)


# --- banknifty risk caps ----------------------------------------------------

def test_banknifty_caps_update_top_level_and_risk_mirror() -> None:
    data = banknifty_config()
    data = apply_risk_caps_banknifty(data, {"max_daily_loss": Decimal("4000"), "max_trade_loss": Decimal("1200")})
    assert data["max_daily_loss"] == 4000
    assert data["max_trade_loss"] == 1200
    assert data["risk"]["max_daily_loss_inr"] == 4000
    assert data["risk"]["max_trade_loss_inr"] == 1200


def test_banknifty_exposure_cannot_exceed_starting_capital() -> None:
    data = banknifty_config()
    too_much = Decimal(str(data["starting_capital"])) + 1
    with pytest.raises(ControlRequestRejected):
        apply_risk_caps_banknifty(data, {"max_premium_exposure": too_much})


def test_every_mirrored_key_has_a_bound() -> None:
    assert set(BANKNIFTY_RISK_MIRROR) == set(RISK_CAP_BOUNDS[ENGINE_BANKNIFTY])


# --- pack risk caps ---------------------------------------------------------

def test_pack_caps_preserve_string_money_types() -> None:
    data = pack_config()
    data = apply_risk_caps_pack(data, "nifty_orb_debit_spread", {"max_trade_loss": Decimal("1200"), "max_trades_per_day": Decimal("2")})
    strat = data["strategies"]["nifty_orb_debit_spread"]
    assert strat["max_trade_loss"] == "1200"  # string, like the rest of the file
    assert strat["max_trades_per_day"] == 2  # int, like the rest of the file


def test_pack_caps_unknown_strategy_rejected() -> None:
    with pytest.raises(ControlRequestRejected):
        apply_risk_caps_pack(pack_config(), "no_such_strategy", {"max_daily_loss": Decimal("4000")})


# --- strategy toggles -------------------------------------------------------

def test_banknifty_toggle_disable_and_reenable_entry_card() -> None:
    data = banknifty_config()
    sid = "banknifty_constituent_led_directional_long_options"
    data = apply_strategy_toggle_banknifty(data, {"strategy_id": sid, "enabled": False, "paper_trade_enabled": False})
    entry = next(item for item in data["strategy_router"] if item["id"] == sid)
    assert entry["enabled"] is False and entry["paper_trade_enabled"] is False
    data = apply_strategy_toggle_banknifty(data, {"strategy_id": sid, "enabled": True, "paper_trade_enabled": True})
    entry = next(item for item in data["strategy_router"] if item["id"] == sid)
    assert entry["enabled"] is True and entry["paper_trade_enabled"] is True


def test_banknifty_toggle_cannot_arm_research_only_or_filter_cards() -> None:
    for sid in (
        "banknifty_official_payoff_structure_selector",
        "options_360_short_straddle_strangle_premium_decay",
        "options_greeks_risk_filter",
        "implied_volatility_regime_filter",
        "trading_psychology_execution_guardrails",
    ):
        with pytest.raises(ControlRequestRejected):
            apply_strategy_toggle_banknifty(banknifty_config(), {"strategy_id": sid, "enabled": True, "paper_trade_enabled": True})


def test_banknifty_toggle_can_disable_any_card() -> None:
    data = apply_strategy_toggle_banknifty(
        banknifty_config(),
        {"strategy_id": "options_360_short_straddle_strangle_premium_decay", "enabled": False, "paper_trade_enabled": False},
    )
    entry = next(item for item in data["strategy_router"] if item["id"] == "options_360_short_straddle_strangle_premium_decay")
    assert entry["enabled"] is False


def test_banknifty_toggle_unknown_strategy_rejected() -> None:
    with pytest.raises(ControlRequestRejected):
        apply_strategy_toggle_banknifty(banknifty_config(), {"strategy_id": "nope", "enabled": True, "paper_trade_enabled": True})


def test_banknifty_toggle_rejects_non_boolean_flags() -> None:
    sid = "banknifty_constituent_led_directional_long_options"
    with pytest.raises(ControlRequestRejected):
        apply_strategy_toggle_banknifty(banknifty_config(), {"strategy_id": sid, "enabled": "yes", "paper_trade_enabled": True})


def test_pack_toggle_known_strategy() -> None:
    data = apply_strategy_toggle_pack(pack_config(), {"strategy_id": "nifty_vwap_mean_reversion", "enabled": False, "paper_trade_enabled": False})
    strat = data["strategies"]["nifty_vwap_mean_reversion"]
    assert strat["enabled"] is False and strat["paper_trade_enabled"] is False


def test_pack_toggle_unknown_strategy_rejected() -> None:
    with pytest.raises(ControlRequestRejected):
        apply_strategy_toggle_pack(pack_config(), {"strategy_id": "nope", "enabled": True, "paper_trade_enabled": True})


# --- atomic write + revalidation --------------------------------------------

def test_write_config_atomically_keeps_original_on_validator_failure(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    original = {"paper_only": True, "value": 1}
    target.write_text(json.dumps(original), encoding="utf-8")

    def failing_validator(path: Path) -> None:
        raise ControlRequestRejected("refused")

    with pytest.raises(ControlRequestRejected):
        write_config_atomically(target, {"paper_only": True, "value": 2}, failing_validator)
    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob(".config_ctl_*")), "temp file must be cleaned up"
    assert not list(tmp_path.glob("*.bak_*")), "no backup should be taken for a refused edit"


def test_write_config_atomically_backs_up_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"value": 1}), encoding="utf-8")
    write_config_atomically(target, {"value": 2}, lambda path: None)
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 2}
    backups = list(tmp_path.glob("config.json.bak_*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"value": 1}


# --- engine validators refuse unsafe edits end-to-end ------------------------

def test_engine_validators_refuse_live_orders_even_if_whitelist_failed(tmp_path: Path) -> None:
    """Defense in depth: even if a bad edit slipped past the whitelist, the
    engine's own load_config refuses paper_only/live_orders_enabled flips."""
    for engine, source in ((ENGINE_BANKNIFTY, banknifty_config()), (ENGINE_PACK, pack_config())):
        data = dict(source)
        data["live_orders_enabled"] = True
        bad = tmp_path / f"{engine}.json"
        bad.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ControlRequestRejected):
            CONFIG_VALIDATORS[engine](bad)


def test_banknifty_validator_refuses_cross_field_violation(tmp_path: Path) -> None:
    data = banknifty_config()
    # 10 trades/day * 1500 per-trade > 5000 daily: engine must refuse.
    data = apply_risk_caps_banknifty(data, {"max_trades_per_day": Decimal("10")})
    bad = tmp_path / "banknifty.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ControlRequestRejected):
        CONFIG_VALIDATORS[ENGINE_BANKNIFTY](bad)


def test_real_configs_pass_their_own_validators() -> None:
    for engine, path in ENGINE_CONFIG_PATHS.items():
        CONFIG_VALIDATORS[engine](path)
