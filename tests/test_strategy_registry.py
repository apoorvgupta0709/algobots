from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.strategy_registry import (  # noqa: E402
    EXECUTABLE_DIRECTIONS,
    EXECUTABLE_STRUCTURES,
    LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL,
    SHORT_PREMIUM_STRUCTURES,
    Desk,
    LifecycleStatus,
    RegistryError,
    load_registry,
    next_lifecycle_status,
    parse_strategy,
    parse_universe,
    strict_bool,
)

REGISTRY_PATH = PROJECT_ROOT / "config" / "strategy_universe_india.json"


def base_executable() -> dict:
    """A minimal valid executable (long/debit) strategy dict."""
    return {
        "id": "x_strat",
        "name": "X Strategy",
        "desk": "equities",
        "family": "orb",
        "instrument": "equity",
        "timeframe": "intraday",
        "direction": "long",
        "structure": "single_leg",
        "executable": True,
        "option_selling": False,
        "lifecycle_status": "backtest_ready",
        "paper_only": True,
        "live_orders_enabled": False,
        "description": "d",
        "entry": "e",
        "exit": "x",
        "filters": [],
        "rationale": "r",
        "data_requirements": [],
        "tags": [],
        "risk": {
            "paper_capital": "50000",
            "max_trade_loss": "1500",
            "max_daily_loss": "5000",
            "max_premium_exposure": "40000",
            "max_trades_per_day": 3,
            "max_open_positions": 1,
        },
    }


def minimal_doc(*strategies: dict, live_orders_enabled: bool = False) -> dict:
    return {
        "schema_version": "1.0",
        "paper_only": True,
        "live_orders_enabled": live_orders_enabled,
        "desks": {"equities": {"name": "Equities", "description": "d"}},
        "strategies": list(strategies),
    }


# --------------------------------------------------------------------------- #
# Real registry integration — the strongest test: the shipped file is safe.
# --------------------------------------------------------------------------- #
def test_real_registry_loads_and_is_paper_only():
    universe = load_registry(REGISTRY_PATH)
    assert universe.paper_only is True
    assert universe.live_orders_enabled is False
    assert len(universe.strategies) >= 30
    assert set(universe.desks) == {Desk.OPTIONS, Desk.EQUITIES, Desk.INVESTMENT}


def test_real_registry_every_strategy_is_safe():
    universe = load_registry(REGISTRY_PATH)
    for s in universe.strategies:
        assert s.paper_only is True, s.id
        assert s.live_orders_enabled is False, s.id
        # Nothing ships pre-labelled as live-eligible; that requires manual approval.
        assert s.lifecycle_status is not LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL, s.id
        if s.option_selling:
            assert s.executable is False, s.id
        if s.structure in SHORT_PREMIUM_STRUCTURES:
            assert s.executable is False, s.id
        if s.executable:
            assert s.structure in EXECUTABLE_STRUCTURES, s.id
            assert s.direction in EXECUTABLE_DIRECTIONS, s.id
            assert s.option_selling is False, s.id
            assert s.risk is not None, s.id
        else:
            assert s.risk is None, s.id


def test_real_registry_has_all_three_desks_populated():
    universe = load_registry(REGISTRY_PATH)
    assert universe.by_desk(Desk.OPTIONS)
    assert universe.by_desk(Desk.EQUITIES)
    assert universe.by_desk(Desk.INVESTMENT)
    assert universe.executable_strategies()
    assert universe.scorecard_strategies()
    # Short-premium families must all be scorecard-only.
    for family in ("straddle", "strangle", "iron_condor", "ratio"):
        for s in universe.by_family(family):
            assert s.executable is False, s.id


# --------------------------------------------------------------------------- #
# strict_bool — string booleans must never pass as flags.
# --------------------------------------------------------------------------- #
def test_strict_bool_rejects_string_true_false():
    assert strict_bool(True, key="k") is True
    assert strict_bool(False, key="k") is False
    with pytest.raises(RegistryError):
        strict_bool("false", key="live_orders_enabled")
    with pytest.raises(RegistryError):
        strict_bool(1, key="paper_only")


# --------------------------------------------------------------------------- #
# Safety rejections in parse_strategy.
# --------------------------------------------------------------------------- #
def test_parse_rejects_executable_option_selling():
    data = base_executable()
    data["option_selling"] = True
    with pytest.raises(RegistryError):
        parse_strategy(data)


def test_parse_rejects_executable_short_premium_structure():
    for structure in ("straddle", "strangle", "iron_condor", "ratio"):
        data = base_executable()
        data["structure"] = structure
        with pytest.raises(RegistryError):
            parse_strategy(data)


def test_parse_rejects_paper_only_false_and_live_true():
    data = base_executable()
    data["paper_only"] = False
    with pytest.raises(RegistryError):
        parse_strategy(data)
    data = base_executable()
    data["live_orders_enabled"] = True
    with pytest.raises(RegistryError):
        parse_strategy(data)


def test_parse_rejects_string_boolean_live_flag():
    data = base_executable()
    data["live_orders_enabled"] = "false"  # bool("false") is True — must be rejected
    with pytest.raises(RegistryError):
        parse_strategy(data)


def test_parse_rejects_executable_without_risk():
    data = base_executable()
    data["risk"] = None
    with pytest.raises(RegistryError):
        parse_strategy(data)


def test_parse_rejects_scorecard_carrying_risk():
    data = base_executable()
    data["executable"] = False  # risk object still present -> must be null for scorecard
    with pytest.raises(RegistryError):
        parse_strategy(data)


def test_parse_accepts_scorecard_short_premium():
    data = base_executable()
    data.update({"executable": False, "option_selling": True, "structure": "iron_condor",
                 "direction": "none", "risk": None})
    strategy = parse_strategy(data)
    assert strategy.executable is False
    assert strategy.scorecard_only is True


def test_parse_accepts_valid_executable():
    strategy = parse_strategy(base_executable())
    assert strategy.executable is True
    assert strategy.risk is not None
    assert strategy.risk.max_trade_loss == Decimal("1500")
    assert isinstance(strategy.risk.max_trade_loss, Decimal)


# --------------------------------------------------------------------------- #
# Universe-level validation.
# --------------------------------------------------------------------------- #
def test_duplicate_ids_rejected():
    with pytest.raises(RegistryError):
        parse_universe(minimal_doc(base_executable(), base_executable()))


def test_universe_rejects_top_level_live_orders_true():
    with pytest.raises(RegistryError):
        parse_universe(minimal_doc(base_executable(), live_orders_enabled=True))


def test_strategy_desk_must_be_declared():
    data = base_executable()
    data["desk"] = "options"  # not among declared desks
    with pytest.raises(RegistryError):
        parse_universe(minimal_doc(data))


# --------------------------------------------------------------------------- #
# Lifecycle ordering.
# --------------------------------------------------------------------------- #
def test_lifecycle_ordering_and_terminal():
    assert next_lifecycle_status(LifecycleStatus.RESEARCH_CANDIDATE) is LifecycleStatus.BACKTEST_READY
    assert next_lifecycle_status(LifecycleStatus.QUALIFIED) is LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL
    assert next_lifecycle_status(LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL) is None


def test_live_eligible_constant_matches_enum():
    assert LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL.value == LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL
