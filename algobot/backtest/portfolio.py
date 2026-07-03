"""Position book and equity accounting for the backtest engine.

A ``BookEntry`` is one logical trade — a single cash/derivative instrument or
a whole multi-leg option structure. Legs of a structure aggregate into ONE
``Trade`` row on close (leg P&L summed; credit structures report as short,
debit as long). Cash is debited/credited per leg fill so per-bar equity is
simply ``cash + sum(leg qty * mark)``.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from algobot.core.enums import ExitReason, Mode, OptionType, ProductType
from algobot.core.models import OptionStructure, Position, Trade

log = logging.getLogger(__name__)

CASH = "CASH"


@dataclass
class Leg:
    """One filled instrument inside a BookEntry. ``qty`` is signed."""
    symbol: str
    qty: int
    entry_price: float
    entry_cost: float
    option_type: str = CASH          # CASH | CE | PE | FUT
    expiry: Optional[dt.date] = None


@dataclass
class BookEntry:
    """An open logical trade with central R-management state."""
    strategy_id: str
    instrument: str                  # underlying / signal instrument
    direction: int                   # +1 long / -1 short on the underlying
    legs: list[Leg]
    entry_time: dt.datetime
    underlying_entry: float
    product_type: ProductType = ProductType.INTRADAY
    stop: Optional[float] = None     # current stop on the underlying
    target: Optional[float] = None
    initial_stop: Optional[float] = None
    trail_anchor: Optional[float] = None   # best favourable underlying price
    be_done: bool = False            # breakeven threshold reached
    stop_ratcheted: bool = False     # stop improved beyond the initial stop
    structure: Optional[OptionStructure] = None
    is_risk_trade: bool = True       # False for REBALANCE accumulation buys
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def display_symbol(self) -> str:
        if self.structure is not None:
            return f"{self.structure.name}:{self.structure.underlying}"
        return self.legs[0].symbol

    @property
    def all_futures(self) -> bool:
        return all(l.option_type == OptionType.FUT.value for l in self.legs)

    def expiries(self) -> list[dt.date]:
        return [l.expiry for l in self.legs if l.expiry is not None]


class PositionBook:
    """Open entries + cash + closed trades; mark-to-market equity."""

    def __init__(self, capital: float):
        self.starting_capital = float(capital)
        self.cash = float(capital)
        self.entries: list[BookEntry] = []
        self.trades: list[Trade] = []

    # ---------------------------------------------------------------- state
    def risk_entry_count(self) -> int:
        return sum(1 for e in self.entries if e.is_risk_trade)

    def entries_for(self, instrument: str) -> list[BookEntry]:
        return [e for e in self.entries if e.instrument == instrument]

    def positions(self) -> list[Position]:
        """Core Position view (one per leg) for StrategyContext.open_positions."""
        out: list[Position] = []
        for e in self.entries:
            for leg in e.legs:
                out.append(Position(
                    strategy_id=e.strategy_id, symbol=leg.symbol, qty=leg.qty,
                    avg_price=leg.entry_price, mode=Mode.BACKTEST,
                    opened_at=e.entry_time, stop_loss=e.stop, take_profit=e.target,
                    underlying=e.instrument, underlying_entry=e.underlying_entry,
                    structure_id=e.entry_id, trail_anchor=e.trail_anchor,
                    product_type=e.product_type))
        return out

    # ---------------------------------------------------------------- open/close
    def open(self, entry: BookEntry) -> None:
        for leg in entry.legs:
            self.cash -= leg.qty * leg.entry_price + leg.entry_cost
        self.entries.append(entry)
        log.debug("OPEN %s dir=%+d legs=%d @%s", entry.display_symbol,
                  entry.direction, len(entry.legs), entry.entry_time)

    def close(self, entry: BookEntry, ts: dt.datetime,
              exit_prices: dict[str, float], exit_costs: dict[str, float],
              reason: ExitReason,
              modeled_exit: Optional[float] = None) -> Trade:
        """Close every leg at ``exit_prices`` and record ONE aggregated Trade."""
        self.entries.remove(entry)
        gross = 0.0
        costs = 0.0
        for leg in entry.legs:
            px = exit_prices[leg.symbol]
            self.cash += leg.qty * px - exit_costs.get(leg.symbol, 0.0)
            gross += leg.qty * (px - leg.entry_price)
            costs += leg.entry_cost + exit_costs.get(leg.symbol, 0.0)

        symbol, direction, qty, entry_px, exit_px = self._aggregate(entry, exit_prices)
        trade = Trade(
            strategy_id=entry.strategy_id, mode=Mode.BACKTEST, symbol=symbol,
            direction=direction, qty=qty,
            entry_time=_pydt(entry.entry_time), exit_time=_pydt(ts),
            entry_price=round(entry_px, 2), exit_price=round(exit_px, 2),
            gross_pnl=round(gross, 2), costs=round(costs, 2),
            net_pnl=round(gross - costs, 2), exit_reason=reason,
            structure_json=entry.structure.to_dict() if entry.structure else None,
            modeled_exit_price=modeled_exit)
        self.trades.append(trade)
        log.debug("CLOSE %s %s pnl=%.0f (%s)", symbol, direction,
                  trade.net_pnl, reason.value)
        return trade

    def _aggregate(self, entry: BookEntry,
                   exit_prices: dict[str, float]) -> tuple[str, str, int, float, float]:
        legs = entry.legs
        if entry.structure is None or (len(legs) == 1 and entry.all_futures):
            leg = legs[0]
            direction = "long" if leg.qty > 0 else "short"
            return (entry.display_symbol, direction, abs(leg.qty),
                    leg.entry_price, exit_prices[leg.symbol])
        # multi-leg structure: net premium per unit, credit -> short / debit -> long
        unit = max(abs(l.qty) for l in legs)
        e_net = sum((l.qty / unit) * l.entry_price for l in legs)
        x_net = sum((l.qty / unit) * exit_prices[l.symbol] for l in legs)
        credit = entry.structure.net_direction == "credit" or e_net < 0
        if credit:
            return entry.display_symbol, "short", unit, -e_net, -x_net
        return entry.display_symbol, "long", unit, e_net, x_net

    # ---------------------------------------------------------------- marking
    def equity(self, mark: Callable[[BookEntry, Leg], float]) -> float:
        value = self.cash
        for e in self.entries:
            for leg in e.legs:
                value += leg.qty * mark(e, leg)
        return value


def _pydt(ts) -> dt.datetime:
    return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
