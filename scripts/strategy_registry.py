#!/usr/bin/env python3
"""Typed registry for the Indian-market strategy universe (paper/research only).

This module loads and validates ``config/strategy_universe_india.json`` into typed
dataclasses. It is the single source of truth for the strategy taxonomy shared by
the qualification engine (``scripts/strategy_qualification.py``) and the read-only
dashboard (``dashboard/strategy_platform_dashboard.py``).

Safety stance (enforced by :func:`validate_strategy`, cannot be bypassed):
- Every strategy must ship ``paper_only: true`` and ``live_orders_enabled: false``.
- Short-premium / undefined-risk structures (short options, straddle, strangle,
  iron condor, ratio) are **scorecard-only**: they are never ``executable``.
- Executable strategies are long / debit-defined-risk only.
- The only "live" lifecycle status is ``live_eligible_requires_manual_approval``,
  which is an *eligibility label granted by a human* — it still ships paper-only and
  no broker order code exists anywhere in this repository.

No FYERS calls, no network, no database. Pure JSON parsing + validation.

CLI:
    uv run python scripts/strategy_registry.py --validate
    uv run python scripts/strategy_registry.py --summary
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "strategy_universe_india.json"

# The one and only "live-ish" status. It is granted by a human via
# scripts/strategy_qualification.py; it does not enable any order placement.
LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL = "live_eligible_requires_manual_approval"


class RegistryError(ValueError):
    """Raised when the strategy registry is malformed or unsafe."""


class Desk(str, Enum):
    OPTIONS = "options"
    EQUITIES = "equities"
    INVESTMENT = "investment"


class Instrument(str, Enum):
    EQUITY = "equity"
    ETF = "etf"
    INDEX_OPTION = "index_option"
    BASKET = "basket"
    PORTFOLIO = "portfolio"


class Timeframe(str, Enum):
    INTRADAY = "intraday"
    SWING = "swing"
    POSITIONAL = "positional"
    LONG_TERM = "long_term"


class Direction(str, Enum):
    LONG = "long"
    LONG_SHORT = "long_short"
    MARKET_NEUTRAL = "market_neutral"
    DIRECTIONAL = "directional"
    NONE = "none"


class Structure(str, Enum):
    SINGLE_LEG = "single_leg"
    DEBIT_SPREAD = "debit_spread"
    STRADDLE = "straddle"
    STRANGLE = "strangle"
    IRON_CONDOR = "iron_condor"
    RATIO = "ratio"
    CALENDAR = "calendar"
    PORTFOLIO = "portfolio"
    NONE = "none"


class LifecycleStatus(str, Enum):
    """Ordered qualification lifecycle. Advances one step at a time."""

    RESEARCH_CANDIDATE = "research_candidate"
    BACKTEST_READY = "backtest_ready"
    BACKTESTED = "backtested"
    PAPER_ENABLED = "paper_enabled"
    PAPER_OBSERVING = "paper_observing"
    QUALIFIED = "qualified"
    LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL = LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL


# Canonical lifecycle progression order.
LIFECYCLE_ORDER: tuple[LifecycleStatus, ...] = (
    LifecycleStatus.RESEARCH_CANDIDATE,
    LifecycleStatus.BACKTEST_READY,
    LifecycleStatus.BACKTESTED,
    LifecycleStatus.PAPER_ENABLED,
    LifecycleStatus.PAPER_OBSERVING,
    LifecycleStatus.QUALIFIED,
    LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL,
)

# Structures that can carry short/undefined-risk premium. Never executable here.
SHORT_PREMIUM_STRUCTURES: frozenset[Structure] = frozenset(
    {Structure.STRADDLE, Structure.STRANGLE, Structure.IRON_CONDOR, Structure.RATIO}
)

# Structures an executable (long/debit) strategy is permitted to use.
EXECUTABLE_STRUCTURES: frozenset[Structure] = frozenset(
    {Structure.SINGLE_LEG, Structure.DEBIT_SPREAD, Structure.PORTFOLIO, Structure.NONE}
)

# Directions an executable strategy is permitted to use (no short-only books).
EXECUTABLE_DIRECTIONS: frozenset[Direction] = frozenset(
    {Direction.LONG, Direction.DIRECTIONAL, Direction.LONG_SHORT, Direction.MARKET_NEUTRAL}
)

_RISK_DECIMAL_KEYS = ("paper_capital", "max_trade_loss", "max_daily_loss", "max_premium_exposure")
_RISK_INT_KEYS = ("max_trades_per_day", "max_open_positions")


def strict_bool(value: Any, *, key: str) -> bool:
    """Safety flags must be literal JSON booleans — ``bool("false")`` is ``True``."""
    if isinstance(value, bool):
        return value
    raise RegistryError(f"{key} must be a JSON boolean true/false, got {value!r}")


def _to_decimal(value: Any, *, key: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RegistryError(f"{key} must be a decimal value, got {value!r}") from exc


def _parse_enum(enum_cls: type[Enum], value: Any, *, key: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_cls)  # type: ignore[attr-defined]
        raise RegistryError(f"{key}={value!r} is not one of: {allowed}") from exc


@dataclass(frozen=True)
class StrategyRisk:
    """Decimal-safe risk caps for an executable strategy."""

    paper_capital: Decimal
    max_trade_loss: Decimal
    max_daily_loss: Decimal
    max_premium_exposure: Decimal
    max_trades_per_day: int
    max_open_positions: int

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, strategy_id: str) -> StrategyRisk:
        risk: dict[str, Any] = {}
        for money_key in _RISK_DECIMAL_KEYS:
            if money_key not in data:
                raise RegistryError(f"strategy {strategy_id}: risk.{money_key} is required for executable strategies")
            amount = _to_decimal(data[money_key], key=f"{strategy_id}.risk.{money_key}")
            if amount < 0:
                raise RegistryError(f"strategy {strategy_id}: risk.{money_key} must be >= 0")
            risk[money_key] = amount
        for int_key in _RISK_INT_KEYS:
            if int_key not in data:
                raise RegistryError(f"strategy {strategy_id}: risk.{int_key} is required for executable strategies")
            try:
                risk[int_key] = int(data[int_key])
            except (TypeError, ValueError) as exc:
                raise RegistryError(f"strategy {strategy_id}: risk.{int_key} must be an integer") from exc
            if risk[int_key] < 0:
                raise RegistryError(f"strategy {strategy_id}: risk.{int_key} must be >= 0")
        return cls(**risk)

    def as_display(self) -> dict[str, str]:
        return {
            "paper_capital": f"{self.paper_capital:.2f}",
            "max_trade_loss": f"{self.max_trade_loss:.2f}",
            "max_daily_loss": f"{self.max_daily_loss:.2f}",
            "max_premium_exposure": f"{self.max_premium_exposure:.2f}",
            "max_trades_per_day": str(self.max_trades_per_day),
            "max_open_positions": str(self.max_open_positions),
        }


@dataclass(frozen=True)
class StrategyDefinition:
    id: str
    name: str
    desk: Desk
    family: str
    instrument: Instrument
    timeframe: Timeframe
    direction: Direction
    structure: Structure
    executable: bool
    option_selling: bool
    lifecycle_status: LifecycleStatus
    paper_only: bool
    live_orders_enabled: bool
    description: str
    entry: str
    exit: str
    filters: tuple[str, ...] = ()
    rationale: str = ""
    data_requirements: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    risk: StrategyRisk | None = None

    @property
    def scorecard_only(self) -> bool:
        return not self.executable

    @property
    def lifecycle_index(self) -> int:
        return LIFECYCLE_ORDER.index(self.lifecycle_status)

    @property
    def is_live_eligible(self) -> bool:
        return self.lifecycle_status is LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL


@dataclass(frozen=True)
class DeskInfo:
    key: Desk
    name: str
    description: str


@dataclass(frozen=True)
class StrategyUniverse:
    schema_version: str
    paper_only: bool
    live_orders_enabled: bool
    notes: str
    desks: dict[Desk, DeskInfo]
    strategies: tuple[StrategyDefinition, ...]

    def by_desk(self, desk: Desk) -> list[StrategyDefinition]:
        return [s for s in self.strategies if s.desk is desk]

    def by_family(self, family: str) -> list[StrategyDefinition]:
        return [s for s in self.strategies if s.family == family]

    def by_id(self, strategy_id: str) -> StrategyDefinition | None:
        for strategy in self.strategies:
            if strategy.id == strategy_id:
                return strategy
        return None

    def executable_strategies(self) -> list[StrategyDefinition]:
        return [s for s in self.strategies if s.executable]

    def scorecard_strategies(self) -> list[StrategyDefinition]:
        return [s for s in self.strategies if not s.executable]

    def families(self) -> list[str]:
        return sorted({s.family for s in self.strategies})

    def lifecycle_histogram(self) -> dict[str, int]:
        histogram = {status.value: 0 for status in LIFECYCLE_ORDER}
        for strategy in self.strategies:
            histogram[strategy.lifecycle_status.value] += 1
        return histogram


def _require_str(data: dict[str, Any], key: str, *, strategy_id: str, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise RegistryError(f"strategy {strategy_id}: {key} must be a string, got {value!r}")
    if not allow_empty and not value.strip():
        raise RegistryError(f"strategy {strategy_id}: {key} must not be empty")
    return value


def _require_str_list(data: dict[str, Any], key: str, *, strategy_id: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RegistryError(f"strategy {strategy_id}: {key} must be a list of strings")
    return tuple(value)


def parse_strategy(data: dict[str, Any]) -> StrategyDefinition:
    if not isinstance(data, dict):
        raise RegistryError(f"strategy entry must be an object, got {type(data).__name__}")
    strategy_id = data.get("id")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise RegistryError(f"strategy id must be a non-empty string, got {strategy_id!r}")

    executable = strict_bool(data.get("executable"), key=f"{strategy_id}.executable")
    risk_raw = data.get("risk")
    risk: StrategyRisk | None = None
    if executable:
        if not isinstance(risk_raw, dict):
            raise RegistryError(f"strategy {strategy_id}: executable strategies require a risk object")
        risk = StrategyRisk.from_dict(risk_raw, strategy_id=strategy_id)
    elif risk_raw not in (None, {}):
        # Scorecard-only strategies must not carry executable risk caps that could
        # be mistaken for an active book.
        raise RegistryError(f"strategy {strategy_id}: non-executable strategy must set risk to null")

    strategy = StrategyDefinition(
        id=strategy_id,
        name=_require_str(data, "name", strategy_id=strategy_id),
        desk=_parse_enum(Desk, data.get("desk"), key=f"{strategy_id}.desk"),
        family=_require_str(data, "family", strategy_id=strategy_id),
        instrument=_parse_enum(Instrument, data.get("instrument"), key=f"{strategy_id}.instrument"),
        timeframe=_parse_enum(Timeframe, data.get("timeframe"), key=f"{strategy_id}.timeframe"),
        direction=_parse_enum(Direction, data.get("direction"), key=f"{strategy_id}.direction"),
        structure=_parse_enum(Structure, data.get("structure"), key=f"{strategy_id}.structure"),
        executable=executable,
        option_selling=strict_bool(data.get("option_selling"), key=f"{strategy_id}.option_selling"),
        lifecycle_status=_parse_enum(LifecycleStatus, data.get("lifecycle_status"), key=f"{strategy_id}.lifecycle_status"),
        paper_only=strict_bool(data.get("paper_only"), key=f"{strategy_id}.paper_only"),
        live_orders_enabled=strict_bool(data.get("live_orders_enabled"), key=f"{strategy_id}.live_orders_enabled"),
        description=_require_str(data, "description", strategy_id=strategy_id),
        entry=_require_str(data, "entry", strategy_id=strategy_id, allow_empty=True),
        exit=_require_str(data, "exit", strategy_id=strategy_id, allow_empty=True),
        filters=_require_str_list(data, "filters", strategy_id=strategy_id),
        rationale=_require_str(data, "rationale", strategy_id=strategy_id, allow_empty=True),
        data_requirements=_require_str_list(data, "data_requirements", strategy_id=strategy_id),
        tags=_require_str_list(data, "tags", strategy_id=strategy_id),
        risk=risk,
    )
    validate_strategy(strategy)
    return strategy


def validate_strategy(strategy: StrategyDefinition) -> None:
    """Enforce the paper-only / long-debit-only safety invariants. Raises RegistryError."""
    sid = strategy.id
    if strategy.paper_only is not True:
        raise RegistryError(f"strategy {sid}: paper_only must be true")
    if strategy.live_orders_enabled is not False:
        raise RegistryError(f"strategy {sid}: live_orders_enabled must be false")

    # Short premium / undefined-risk is scorecard-only, never executable.
    if strategy.option_selling and strategy.executable:
        raise RegistryError(f"strategy {sid}: option-selling (short premium) strategies must not be executable")
    if strategy.structure in SHORT_PREMIUM_STRUCTURES and strategy.executable:
        raise RegistryError(
            f"strategy {sid}: {strategy.structure.value} is an undefined/short-risk structure and must be scorecard-only (executable=false)"
        )

    if strategy.executable:
        if strategy.structure not in EXECUTABLE_STRUCTURES:
            raise RegistryError(
                f"strategy {sid}: executable strategies must use a long/debit structure ({', '.join(s.value for s in sorted(EXECUTABLE_STRUCTURES, key=lambda x: x.value))})"
            )
        if strategy.direction not in EXECUTABLE_DIRECTIONS:
            raise RegistryError(f"strategy {sid}: executable strategies must not be short-only ({strategy.direction.value})")
        if strategy.risk is None:
            raise RegistryError(f"strategy {sid}: executable strategies require risk caps")

    # An entry may be *labelled* live-eligible only after manual approval, but it
    # must still ship paper-only with no live order flag (checked above).
    if strategy.is_live_eligible and (strategy.paper_only is not True or strategy.live_orders_enabled is not False):
        raise RegistryError(f"strategy {sid}: live-eligible label still requires paper_only=true, live_orders_enabled=false")


def parse_universe(data: dict[str, Any]) -> StrategyUniverse:
    if not isinstance(data, dict):
        raise RegistryError("registry root must be a JSON object")
    if strict_bool(data.get("paper_only"), key="paper_only") is not True:
        raise RegistryError("registry paper_only must be true")
    if strict_bool(data.get("live_orders_enabled"), key="live_orders_enabled") is not False:
        raise RegistryError("registry live_orders_enabled must be false")

    desks_raw = data.get("desks")
    if not isinstance(desks_raw, dict) or not desks_raw:
        raise RegistryError("registry desks must be a non-empty object")
    desks: dict[Desk, DeskInfo] = {}
    for desk_key, info in desks_raw.items():
        desk = _parse_enum(Desk, desk_key, key="desks")
        if not isinstance(info, dict):
            raise RegistryError(f"desk {desk_key}: info must be an object")
        desks[desk] = DeskInfo(
            key=desk,
            name=str(info.get("name") or desk.value),
            description=str(info.get("description") or ""),
        )

    strategies_raw = data.get("strategies")
    if not isinstance(strategies_raw, list) or not strategies_raw:
        raise RegistryError("registry strategies must be a non-empty list")

    strategies: list[StrategyDefinition] = []
    seen_ids: set[str] = set()
    for entry in strategies_raw:
        strategy = parse_strategy(entry)
        if strategy.id in seen_ids:
            raise RegistryError(f"duplicate strategy id: {strategy.id}")
        seen_ids.add(strategy.id)
        if strategy.desk not in desks:
            raise RegistryError(f"strategy {strategy.id}: desk {strategy.desk.value} is not declared in desks")
        strategies.append(strategy)

    return StrategyUniverse(
        schema_version=str(data.get("schema_version") or "1.0"),
        paper_only=True,
        live_orders_enabled=False,
        notes=str(data.get("notes") or ""),
        desks=desks,
        strategies=tuple(strategies),
    )


def load_registry(path: Path | str = DEFAULT_REGISTRY_PATH) -> StrategyUniverse:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"strategy registry not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"invalid JSON in {path}: {exc}") from exc
    return parse_universe(data)


def next_lifecycle_status(status: LifecycleStatus) -> LifecycleStatus | None:
    """Return the next lifecycle stage, or None if already at the terminal stage."""
    idx = LIFECYCLE_ORDER.index(status)
    if idx + 1 >= len(LIFECYCLE_ORDER):
        return None
    return LIFECYCLE_ORDER[idx + 1]


def summary_lines(universe: StrategyUniverse) -> list[str]:
    lines = [
        "## India Strategy Universe",
        f"schema_version: {universe.schema_version}",
        f"paper_only: {universe.paper_only} · live_orders_enabled: {universe.live_orders_enabled}",
        f"total strategies: {len(universe.strategies)}",
        "",
        "### By desk",
    ]
    for desk in Desk:
        desk_strategies = universe.by_desk(desk)
        if not desk_strategies:
            continue
        executable = sum(1 for s in desk_strategies if s.executable)
        lines.append(
            f"- {desk.value}: {len(desk_strategies)} strategies "
            f"({executable} executable, {len(desk_strategies) - executable} scorecard-only)"
        )
    lines.extend(["", "### Lifecycle distribution"])
    for status, count in universe.lifecycle_histogram().items():
        if count:
            lines.append(f"- {status}: {count}")
    lines.extend(["", f"### Families ({len(universe.families())})", ", ".join(universe.families())])
    return lines


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate / summarize the India strategy universe (paper-only)")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--validate", action="store_true", help="validate the registry and exit non-zero on failure")
    parser.add_argument("--summary", action="store_true", help="print a human summary of the universe")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        universe = load_registry(args.registry)
    except RegistryError as exc:
        print(f"INVALID: {exc}")
        return 1
    if args.summary or not args.validate:
        print("\n".join(summary_lines(universe)))
    if args.validate:
        print(f"OK: {len(universe.strategies)} strategies validated (paper-only, no live orders).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
