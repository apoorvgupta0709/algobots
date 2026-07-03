"""The strategy plugin contract.

Every strategy is ONE self-contained file under ``algobot/strategies/<category>/``
containing exactly one ``StrategyBase`` subclass whose ``meta.strategy_id`` equals
the file name (e.g. ``id01_orb``). The registry auto-discovers it; no other file
needs editing to add a strategy.

Rules (enforced by tests/test_contract.py):
- ``generate_signals`` is a pure function of ``(data, ctx)``: no network, no file
  writes, no broker access, deterministic.
- The LAST row of every DataFrame in ``data`` is the most recent *closed* bar.
  Never assume the forming bar is present. Appending future rows must not change
  signals for past bars (no look-ahead).
- Strategies never size positions or place orders — they emit Signals; the
  OrderManager does risk sizing (0.5-1% per trade from stop distance), caps and
  routing.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable, ClassVar, Optional

import pandas as pd

from algobot.core.enums import Category, Timeframe
from algobot.core.models import Position, Signal

if TYPE_CHECKING:
    from algobot.options.chain import OptionChain
    from algobot.options.leg_builder import LegBuilder


# Scan schedule tokens understood by the engine scheduler.
SCAN_EVERY_5MIN = "every_5min"    # intraday scan loop, 09:20-15:10 IST on closed 5-min bars
SCAN_EVERY_15MIN = "every_15min"
SCAN_0920_ONCE = "0920_once"      # once shortly after open (e.g. 9:20 straddle, gap plays)
SCAN_EOD = "eod"                  # ~15:45 IST on daily bars (swing/positional/screens)
SCAN_WEEKLY = "weekly"            # first trading day of the week, EOD data
SCAN_MONTHLY = "monthly"          # first trading day of the month (SIP, rebalance, factors)
SCAN_EXPIRY_DAY = "expiry_day"    # weekly-expiry sessions only, 5-min cadence

VALID_SCHEDULES = {
    SCAN_EVERY_5MIN, SCAN_EVERY_15MIN, SCAN_0920_ONCE, SCAN_EOD,
    SCAN_WEEKLY, SCAN_MONTHLY, SCAN_EXPIRY_DAY,
}


@dataclass(frozen=True)
class StrategyMeta:
    strategy_id: str                  # must equal the module file name
    name: str                         # human-readable, e.g. "Opening Range Breakout"
    category: Category
    timeframe: Timeframe              # primary decision timeframe
    scan_schedule: str                # one of the SCAN_* tokens above
    instruments: list[str]            # concrete symbols or a universe key (see universes.py)
    warmup_bars: int                  # min history rows needed before first signal
    params: dict = field(default_factory=dict)   # defaults; overridable via config/DB
    capital_required: float = 100_000.0          # min capital incl. margin for short options
    max_positions: int = 1
    max_trades_per_day: int = 3
    intraday_squareoff: bool = False  # True => engine flattens by 15:20 IST
    is_multi_leg: bool = False
    is_pair: bool = False
    description: str = ""             # 1-3 lines: edge, regime, risk (from the compendium)


@dataclass
class StrategyContext:
    """Everything a strategy may look at beyond candles. Read-only by convention.

    ``option_chain`` and ``leg_builder`` are provided by the runtime (live/paper)
    and by the backtester (synthetic chain). ``extras`` carries optional data
    like fundamentals frames for screen strategies.
    """
    now: datetime                                  # tz-aware IST
    open_positions: list[Position] = field(default_factory=list)   # THIS strategy's only
    capital_allocated: float = 100_000.0
    option_chain: Optional[Callable[[str], "OptionChain"]] = None  # underlying -> chain
    leg_builder: Optional["LegBuilder"] = None
    trades_today: int = 0
    extras: dict = field(default_factory=dict)

    @property
    def has_open_position(self) -> bool:
        return len(self.open_positions) > 0


class StrategyBase(ABC):
    """Subclass this; set ``meta``; implement ``generate_signals``."""

    meta: ClassVar[StrategyMeta]

    def __init__(self, params: Optional[dict] = None):
        merged = dict(self.meta.params)
        merged.update(params or {})
        self.params = merged

    # ------------------------------------------------------------------ required
    @abstractmethod
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        """Return entry/exit signals for the just-closed bar.

        ``data`` maps symbol -> OHLCV DataFrame with a tz-aware IST DatetimeIndex
        and columns [open, high, low, close, volume]; last row = latest CLOSED bar.
        Return [] when there is nothing to do.
        """

    # ------------------------------------------------------------------ optional
    def on_position_update(self, position: Position, ltp_map: dict[str, float],
                           ctx: StrategyContext) -> list[Signal]:
        """Called from the 15-second monitor loop for strategies needing dynamic
        management (gamma re-hedge, strangle defense). Static SL/TP/trailing is
        handled centrally — do NOT reimplement it here. Default: no action."""
        return []

    def universe(self, ctx: StrategyContext) -> list[str]:
        """Symbols to fetch data for this scan. Override for dynamic universes
        (relative-strength ranks, screens). Default: meta.instruments resolved
        by the runner through universes.resolve()."""
        return list(self.meta.instruments)

    # ------------------------------------------------------------------ helpers
    @property
    def strategy_id(self) -> str:
        return self.meta.strategy_id

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Strategy {self.meta.strategy_id} ({self.meta.category.value})>"
