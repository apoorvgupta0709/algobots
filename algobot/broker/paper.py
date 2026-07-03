"""Paper broker: instant simulated fills with the real Indian cost stack.

Fills MARKET orders at the live quote worsened by the segment slippage model,
persists orders/fills, maintains signed positions and writes journal TradeRows
on round-trip close — so paper results are directly comparable to backtests
and to live fills (same CostModel, same tables).
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Callable, Optional

from algobot.broker.base import BrokerInterface
from algobot.core.clock import now_ist
from algobot.core.config import settings
from algobot.core.enums import ExitReason, Mode, OrderStatus, OrderType, ProductType, Side
from algobot.core.models import Order, Position
from algobot.costs.india import CostModel
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import FillRow, OrderRow, PositionRow, TradeRow

log = logging.getLogger(__name__)

QuoteFn = Callable[[list[str]], dict[str, float]]


def _now() -> dt.datetime:
    """Naive IST timestamp for DB columns."""
    return now_ist().replace(tzinfo=None)


def _exit_reason_from_tag(tag: str | None) -> str:
    """Map an order tag to an ExitReason value.

    Accepts a bare reason (``"sl"``), a namespaced tag (``"id01_orb:tp"``) or
    anything else (falls back to ``"signal"``).
    """
    if tag:
        for candidate in (tag, tag.rsplit(":", 1)[-1]):
            try:
                return ExitReason(candidate).value
            except ValueError:
                continue
    return ExitReason.SIGNAL.value


class PaperBroker(BrokerInterface):
    """Simulated broker. All rows are written with ``mode='paper'``."""

    def __init__(self, quote_fn: Optional[QuoteFn] = None,
                 cost_model: Optional[CostModel] = None):
        self.quote_fn = quote_fn
        self.cost_model = cost_model or CostModel()
        init_db()

    @property
    def name(self) -> str:
        return "paper"

    # ------------------------------------------------------------------ quotes
    def _quote(self, symbol: str) -> Optional[float]:
        if self.quote_fn is None:
            return None
        try:
            return self.quote_fn([symbol]).get(symbol)
        except Exception:
            log.exception("paper quote_fn failed for %s", symbol)
            return None

    def _reference_price(self, order: Order, session) -> Optional[float]:
        """Fallback chain: live quote -> limit -> stop -> last known position price."""
        price = self._quote(order.symbol)
        if price is None:
            price = order.limit_price or order.stop_price
        if price is None:
            row = (session.query(PositionRow)
                   .filter_by(mode=Mode.PAPER.value, symbol=order.symbol)
                   .order_by(PositionRow.id.desc()).first())
            if row is not None:
                price = row.last_price or row.avg_price
        return price

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        if self.quote_fn is not None:
            try:
                return dict(self.quote_fn(symbols))
            except Exception:
                log.exception("paper quote_fn failed for %s", symbols)
        out: dict[str, float] = {}
        with session_scope() as s:
            for sym in symbols:
                row = (s.query(PositionRow)
                       .filter_by(mode=Mode.PAPER.value, symbol=sym)
                       .order_by(PositionRow.id.desc()).first())
                if row is not None:
                    out[sym] = row.last_price or row.avg_price
        return out

    # ------------------------------------------------------------------ orders
    def place_order(self, order: Order) -> Order:
        """Fill (or park) the order and persist all resulting rows atomically."""
        now = _now()
        with session_scope() as s:
            ref = self._reference_price(order, s)
            order.broker_order_id = f"P{uuid.uuid4().hex[:10]}"

            fill_price = self._fill_price(order, ref)
            row = OrderRow(
                signal_id=order.signal_id,
                strategy_id=order.strategy_id,
                mode=Mode.PAPER.value,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                side=int(order.side),
                qty=int(order.qty),
                order_type=int(order.order_type),
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                product_type=order.product_type.value,
                tag=order.tag or None,
                ts_placed=now,
            )
            if fill_price is None:
                # Resting limit order, or no price reference at all.
                row.status = (OrderStatus.PLACED.value if ref is not None
                              else OrderStatus.REJECTED.value)
                order.status = OrderStatus(row.status)
                s.add(row)
                s.flush()
                order.id = row.id
                if order.status == OrderStatus.REJECTED:
                    log.warning("paper order rejected (no price reference): %s",
                                order.symbol)
                return order

            row.status = OrderStatus.FILLED.value
            row.ts_filled = now
            s.add(row)
            s.flush()
            order.id = row.id
            order.status = OrderStatus.FILLED
            s.add(FillRow(order_id=row.id, price=fill_price, qty=int(order.qty), ts=now))
            self._apply_fill(s, order, fill_price, now)
        log.info("paper fill %s %s x%d @ %.2f", order.side.name, order.symbol,
                 order.qty, fill_price)
        return order

    def _fill_price(self, order: Order, ref: Optional[float]) -> Optional[float]:
        """Fill price with slippage, or None when the order cannot fill now."""
        if ref is None:
            return None
        slipped = self.cost_model.apply_slippage(order.symbol, order.side, ref,
                                                 order.product_type)
        if order.order_type == OrderType.LIMIT:
            limit = order.limit_price
            if limit is None:
                return slipped
            # Marketable check against the raw quote; fill never worse than limit.
            if order.side == Side.BUY:
                return min(slipped, limit) if ref <= limit else None
            return max(slipped, limit) if ref >= limit else None
        # MARKET / STOP / STOP_LIMIT are simulated as immediate market fills.
        return slipped

    def cancel_order(self, broker_order_id: str) -> bool:
        with session_scope() as s:
            row = (s.query(OrderRow)
                   .filter_by(broker_order_id=broker_order_id,
                              mode=Mode.PAPER.value).first())
            if row is None or row.status not in (OrderStatus.PLACED.value,
                                                 OrderStatus.PENDING.value):
                return False
            row.status = OrderStatus.CANCELLED.value
        return True

    # ------------------------------------------------------------------ positions
    def _apply_fill(self, s, order: Order, fill_price: float, now: dt.datetime) -> None:
        """Update the signed position book: open / add / reduce / close / flip."""
        delta = int(order.side) * int(order.qty)
        pos = (s.query(PositionRow)
               .filter_by(mode=Mode.PAPER.value, strategy_id=order.strategy_id,
                          symbol=order.symbol, status="open").first())

        if pos is None:
            s.add(PositionRow(
                strategy_id=order.strategy_id, mode=Mode.PAPER.value,
                symbol=order.symbol, qty=delta, avg_price=fill_price,
                product_type=order.product_type.value, opened_at=now,
                last_price=fill_price, unrealized_pnl=0.0))
            return

        if pos.qty * delta > 0:  # add to the same side: weighted average entry
            total = pos.qty + delta
            pos.avg_price = (pos.avg_price * pos.qty + fill_price * delta) / total
            pos.qty = total
            pos.last_price = fill_price
            return

        closed_qty = min(abs(delta), abs(pos.qty))
        self._write_trade(s, pos, order, closed_qty, fill_price, now)

        remainder = pos.qty + delta
        if remainder == 0:                       # full close
            pos.qty = 0
            pos.status = "closed"
            pos.last_price = fill_price
        elif remainder * pos.qty > 0:            # partial reduce, same side remains
            pos.qty = remainder
            pos.last_price = fill_price
        else:                                    # flip: close old, open reverse
            pos.qty = 0
            pos.status = "closed"
            pos.last_price = fill_price
            s.add(PositionRow(
                strategy_id=order.strategy_id, mode=Mode.PAPER.value,
                symbol=order.symbol, qty=remainder, avg_price=fill_price,
                product_type=order.product_type.value, opened_at=now,
                last_price=fill_price, unrealized_pnl=0.0))

    def _write_trade(self, s, pos: PositionRow, order: Order, closed_qty: int,
                     exit_price: float, now: dt.datetime) -> None:
        """Journal the closed round trip with both-side costs."""
        long_trade = pos.qty > 0
        product = ProductType(pos.product_type)
        entry_side = Side.BUY if long_trade else Side.SELL
        exit_side = Side.SELL if long_trade else Side.BUY
        gross = (exit_price - pos.avg_price) * closed_qty * (1 if long_trade else -1)
        costs = (self.cost_model.order_costs(pos.symbol, entry_side, closed_qty,
                                             pos.avg_price, product)
                 + self.cost_model.order_costs(pos.symbol, exit_side, closed_qty,
                                               exit_price, product))
        s.add(TradeRow(
            strategy_id=pos.strategy_id, mode=Mode.PAPER.value, symbol=pos.symbol,
            direction="long" if long_trade else "short", qty=closed_qty,
            entry_time=pos.opened_at, exit_time=now,
            entry_price=pos.avg_price, exit_price=exit_price,
            gross_pnl=round(gross, 2), costs=round(costs, 2),
            net_pnl=round(gross - costs, 2),
            exit_reason=_exit_reason_from_tag(order.tag),
            structure_json=pos.structure_json,
        ))

    def get_positions(self) -> list[Position]:
        with session_scope() as s:
            rows = s.query(PositionRow).filter_by(mode=Mode.PAPER.value,
                                                  status="open").all()
            return [Position(
                strategy_id=r.strategy_id, symbol=r.symbol, qty=r.qty,
                avg_price=r.avg_price, mode=Mode.PAPER, opened_at=r.opened_at,
                stop_loss=r.stop_loss, take_profit=r.take_profit,
                underlying=r.underlying, underlying_entry=r.underlying_entry,
                structure_id=r.structure_id, trail_anchor=r.trail_anchor,
                product_type=ProductType(r.product_type), id=r.id,
            ) for r in rows]

    def get_funds(self) -> float:
        """Configured capital minus notionals locked in open paper positions."""
        capital = float(settings()["capital"])
        with session_scope() as s:
            rows = s.query(PositionRow).filter_by(mode=Mode.PAPER.value,
                                                  status="open").all()
            used = sum(abs(r.qty) * r.avg_price for r in rows)
        return capital - used
