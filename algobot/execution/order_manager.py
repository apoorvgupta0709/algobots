"""OrderManager: the only path from Signals to Orders.

Responsibilities: persist the signal, run risk checks, size from stop distance,
resolve option structures into concrete legs, route to the right broker for
the mode, and stamp SL/TP/underlying metadata onto position rows so the
PositionMonitor can manage them centrally.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from algobot.broker.base import BrokerInterface
from algobot.core.clock import now_ist
from algobot.core.enums import (
    ExitReason,
    Mode,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    SignalType,
)
from algobot.core.exceptions import BrokerError, RiskRejection
from algobot.core.models import Order, Signal
from algobot.core.universes import lot_size
from algobot.costs.india import CostModel
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import EventLogRow, PositionRow, SignalRow, TradeRow

log = logging.getLogger(__name__)


def _now():
    return now_ist().replace(tzinfo=None)


class OrderManager:
    """Convert signals to orders and manage position lifecycle in the DB."""

    def __init__(self, brokers: dict[Mode, BrokerInterface], risk, cost_model=None,
                 leg_builder=None):
        self.brokers = brokers
        self.risk = risk
        self.cost_model = cost_model or CostModel()
        self.leg_builder = leg_builder
        init_db()

    # ------------------------------------------------------------------ submit
    def submit(self, signal: Signal, mode: Mode, capital: float) -> list[Order]:
        """Process one signal end to end; returns the orders that were placed."""
        signal_id = self._persist_signal(signal)

        if signal.signal_type == SignalType.EXIT:
            orders = self._handle_exit_signal(signal, mode, signal_id)
            self._mark_signal(signal_id, "executed")
            return orders

        open_count = self._open_position_count(mode)
        try:
            self.risk.check(signal, mode, open_count, self.risk.day_state())
        except RiskRejection as exc:
            log.info("signal rejected [%s]: %s", signal.strategy_id, exc)
            self._mark_signal(signal_id, "rejected", str(exc))
            return []

        try:
            orders = self._build_orders(signal, mode, capital, signal_id)
        except Exception as exc:
            self._mark_signal(signal_id, "rejected", f"order build failed: {exc}")
            raise
        if not orders:
            self._mark_signal(signal_id, "rejected", "sized to zero quantity")
            return []

        broker = self.brokers[mode]
        placed: list[Order] = []
        for order in orders:
            try:
                placed.append(broker.place_order(order))
            except BrokerError as exc:
                self._journal("error", f"broker rejected {order.symbol}: {exc}")
                self._mark_signal(signal_id, "rejected", str(exc))
                return placed

        self._stamp_positions(signal, mode, placed, broker)
        self._mark_signal(signal_id, "executed")
        self.risk.update_day_state(trades_delta=1,
                                   positions_count=self._open_position_count(mode))
        return placed

    # ------------------------------------------------------------------ order building
    def _build_orders(self, signal: Signal, mode: Mode, capital: float,
                      signal_id: int) -> list[Order]:
        if signal.structure is not None:
            return self._structure_orders(signal, mode, capital, signal_id)
        return self._single_orders(signal, mode, capital, signal_id)

    def _structure_orders(self, signal: Signal, mode: Mode, capital: float,
                          signal_id: int) -> list[Order]:
        """One order per resolved option leg, sharing a structure_id.

        Structures with a short leg are margin-sized against the risk budget
        via the same helper the backtest uses, so paper/live risk matches the
        risk profile the backtest evaluated. Pure-long (debit) structures
        keep the strategy's leg.lots (their max loss is the debit paid).
        """
        builder = self.leg_builder
        if builder is None:
            from algobot.options.leg_builder import LegBuilder  # lazy: optional dep
            builder = LegBuilder()
        structure = builder.resolve(signal.structure, spot=signal.reference_price,
                                    now=signal.timestamp)
        structure_id = uuid.uuid4().hex
        per_lot = lot_size(structure.underlying)
        sized_lots = 1
        if any(leg.side == Side.SELL for leg in structure.legs):
            from algobot.options.sizing import structure_lots
            risk_amt = capital * float(self.risk.cfg["risk_per_trade_pct"]) / 100.0
            sized_lots = structure_lots(structure, float(signal.reference_price),
                                        per_lot, risk_amt, capital)
            if sized_lots <= 0:
                self._journal(
                    "warn",
                    f"{signal.strategy_id}: {structure.name} margin/lot exceeds "
                    f"capital {capital:.0f} — structure sized to zero")
                return []
        orders = []
        for leg in structure.legs:
            if not leg.resolved_symbol:
                raise BrokerError(f"leg builder left a leg unresolved in "
                                  f"{structure.name} ({signal.strategy_id})")
            orders.append(Order(
                strategy_id=signal.strategy_id, symbol=leg.resolved_symbol,
                side=leg.side, qty=leg.lots * sized_lots * per_lot,
                order_type=OrderType.MARKET, product_type=signal.product_type,
                mode=mode, tag=f"{signal.strategy_id}:{structure_id}",
                signal_id=signal_id,
            ))
        # Remember for stamping: every leg shares this structure id.
        signal.tags["structure_id"] = structure_id
        signal.tags["resolved_structure"] = structure.to_dict()
        return orders

    def _single_orders(self, signal: Signal, mode: Mode, capital: float,
                       signal_id: int) -> list[Order]:
        qty = self._sized_qty(signal, capital)
        if qty <= 0:
            return []
        side = Side.BUY if signal.signal_type != SignalType.ENTRY_SHORT else Side.SELL
        orders = [Order(
            strategy_id=signal.strategy_id, symbol=signal.instrument, side=side,
            qty=qty, order_type=OrderType.MARKET, product_type=signal.product_type,
            mode=mode, tag=signal.strategy_id, signal_id=signal_id,
        )]
        if signal.pair_leg is not None:
            hedge_qty = max(1, round(qty * signal.pair_leg.hedge_ratio))
            orders.append(Order(
                strategy_id=signal.strategy_id, symbol=signal.pair_leg.symbol,
                side=Side.SELL if side == Side.BUY else Side.BUY, qty=hedge_qty,
                order_type=OrderType.MARKET, product_type=signal.product_type,
                mode=mode, tag=f"{signal.strategy_id}:pair", signal_id=signal_id,
            ))
        return orders

    def _sized_qty(self, signal: Signal, capital: float) -> int:
        lot = lot_size(signal.instrument)
        if signal.stop_loss is not None:
            return self.risk.size_qty(signal.reference_price, signal.stop_loss,
                                      capital, lot=lot)
        hint = signal.size_hint
        if hint is None:
            return 0
        if hint.qty is not None:
            return hint.qty * lot if lot > 1 else hint.qty
        notional = (capital * hint.weight if hint.weight is not None
                    else hint.notional)
        if not notional or signal.reference_price <= 0:
            return 0
        return int(notional / signal.reference_price // lot) * lot

    # ------------------------------------------------------------------ stamping
    def _stamp_positions(self, signal: Signal, mode: Mode, orders: list[Order],
                         broker: BrokerInterface) -> None:
        """Attach SL/TP/underlying/structure metadata to each order's position row.

        Paper broker maintains position rows itself; for brokers that do not
        (live), the row is created here from the best available price.
        """
        structure_id = signal.tags.get("structure_id")
        structure_json = signal.tags.get("resolved_structure") or (
            signal.structure.to_dict() if signal.structure else None)
        underlying = (signal.structure.underlying if signal.structure
                      else signal.instrument)
        now = _now()
        with session_scope() as s:
            for order in orders:
                if order.status not in (OrderStatus.FILLED, OrderStatus.PLACED,
                                        OrderStatus.PARTIAL):
                    continue
                row = (s.query(PositionRow)
                       .filter_by(mode=mode.value, strategy_id=order.strategy_id,
                                  symbol=order.symbol, status="open").first())
                if row is None:  # live broker: DB row is ours to create
                    row = PositionRow(
                        strategy_id=order.strategy_id, mode=mode.value,
                        symbol=order.symbol, qty=int(order.side) * order.qty,
                        avg_price=self._best_price(broker, order, signal),
                        product_type=order.product_type.value, opened_at=now)
                    s.add(row)
                primary = signal.structure is not None or order.symbol == signal.instrument
                if primary:
                    row.stop_loss = signal.stop_loss
                    row.take_profit = signal.take_profit
                row.underlying = underlying
                row.underlying_entry = signal.reference_price
                row.structure_id = structure_id
                row.structure_json = structure_json

    @staticmethod
    def _best_price(broker: BrokerInterface, order: Order, signal: Signal) -> float:
        try:
            quote = broker.get_quotes([order.symbol]).get(order.symbol)
        except Exception:
            quote = None
        if quote:
            return float(quote)
        if order.limit_price:
            return float(order.limit_price)
        return float(signal.reference_price) if order.symbol == signal.instrument else 0.0

    # ------------------------------------------------------------------ closing
    def close_position(self, position_id: int, reason: ExitReason,
                       modeled_exit_price: Optional[float] = None,
                       signal_id: Optional[int] = None) -> Optional[Order]:
        """Close one position with a reverse-side market order.

        The public exit primitive: guarantees a TradeRow lands (with
        ``modeled_exit_price`` stamped) and rolls realized P&L into day state.
        """
        with session_scope() as s:
            row = s.get(PositionRow, position_id)
            if row is None or row.status != "open" or row.qty == 0:
                return None
            mode = Mode(row.mode)
            order = Order(
                strategy_id=row.strategy_id, symbol=row.symbol,
                side=Side.SELL if row.qty > 0 else Side.BUY, qty=abs(row.qty),
                order_type=OrderType.MARKET,
                product_type=ProductType(row.product_type), mode=mode,
                tag=f"{row.strategy_id}:{reason.value}", signal_id=signal_id,
            )

        broker = self.brokers[mode]
        broker.place_order(order)

        net_pnl = self._finalize_close(position_id, order, reason,
                                       modeled_exit_price, broker)
        self.risk.update_day_state(
            pnl_delta=net_pnl,
            positions_count=self._open_position_count(mode))
        log.info("closed position %d (%s) reason=%s net=%.2f",
                 position_id, order.symbol, reason.value, net_pnl)
        return order

    def _finalize_close(self, position_id: int, order: Order, reason: ExitReason,
                        modeled_exit_price: Optional[float],
                        broker: BrokerInterface) -> float:
        """Ensure the position row is closed and a TradeRow exists; return net P&L."""
        with session_scope() as s:
            row = s.get(PositionRow, position_id)
            trade = (s.query(TradeRow)
                     .filter_by(strategy_id=row.strategy_id, mode=row.mode,
                                symbol=row.symbol)
                     .order_by(TradeRow.id.desc()).first())
            if row.status == "open":
                # Broker keeps its own book (live): write the round trip ourselves.
                exit_price = modeled_exit_price
                if exit_price is None:
                    try:
                        exit_price = broker.get_quotes([row.symbol]).get(row.symbol)
                    except Exception:
                        exit_price = None
                exit_price = exit_price or row.last_price or row.avg_price
                long_trade = row.qty > 0
                qty = abs(row.qty)
                product = ProductType(row.product_type)
                gross = (exit_price - row.avg_price) * qty * (1 if long_trade else -1)
                costs = (self.cost_model.order_costs(
                            row.symbol, Side.BUY if long_trade else Side.SELL,
                            qty, row.avg_price, product)
                         + self.cost_model.order_costs(
                            row.symbol, Side.SELL if long_trade else Side.BUY,
                            qty, exit_price, product))
                trade = TradeRow(
                    strategy_id=row.strategy_id, mode=row.mode, symbol=row.symbol,
                    direction="long" if long_trade else "short", qty=qty,
                    entry_time=row.opened_at, exit_time=_now(),
                    entry_price=row.avg_price, exit_price=exit_price,
                    gross_pnl=round(gross, 2), costs=round(costs, 2),
                    net_pnl=round(gross - costs, 2), exit_reason=reason.value,
                    structure_json=row.structure_json)
                s.add(trade)
                row.qty = 0
                row.status = "closed"
                row.last_price = exit_price
            if trade is not None and modeled_exit_price is not None:
                trade.modeled_exit_price = modeled_exit_price
            s.flush()
            return float(trade.net_pnl) if trade is not None else 0.0

    def flatten(self, strategy_id: Optional[str], mode: Mode,
                reason: ExitReason) -> list[Order]:
        """Close every open position (optionally one strategy's) in ``mode``.

        Realized P&L rolls into the day state via :meth:`close_position`.
        """
        with session_scope() as s:
            q = s.query(PositionRow.id).filter_by(mode=mode.value, status="open")
            if strategy_id:
                q = q.filter_by(strategy_id=strategy_id)
            ids = [r[0] for r in q.all()]
        orders = []
        for pid in ids:
            try:
                order = self.close_position(pid, reason)
                if order is not None:
                    orders.append(order)
            except Exception as exc:  # keep flattening the rest of the book
                log.exception("flatten failed for position %d", pid)
                self._journal("error", f"flatten failed for position {pid}: {exc}")
        return orders

    # ------------------------------------------------------------------ persistence
    @staticmethod
    def _persist_signal(signal: Signal) -> int:
        ts = signal.timestamp.replace(tzinfo=None) if signal.timestamp.tzinfo \
            else signal.timestamp
        with session_scope() as s:
            row = SignalRow(
                strategy_id=signal.strategy_id, ts=ts,
                signal_type=signal.signal_type.value, instrument=signal.instrument,
                reference_price=signal.reference_price, stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                structure_json=signal.structure.to_dict() if signal.structure else None,
                status="emitted", reason=signal.reason or None)
            s.add(row)
            s.flush()
            return row.id

    @staticmethod
    def _mark_signal(signal_id: int, status: str, reject_reason: str | None = None):
        with session_scope() as s:
            row = s.get(SignalRow, signal_id)
            if row is not None:
                row.status = status
                row.reject_reason = reject_reason

    @staticmethod
    def _open_position_count(mode: Mode) -> int:
        with session_scope() as s:
            return (s.query(PositionRow)
                    .filter_by(mode=mode.value, status="open").count())

    @staticmethod
    def _journal(level: str, message: str) -> None:
        try:
            with session_scope() as s:
                s.add(EventLogRow(level=level, source="order_manager", message=message))
        except Exception:
            log.exception("failed to journal: %s", message)

    # ------------------------------------------------------------------ exit signals
    def _handle_exit_signal(self, signal: Signal, mode: Mode,
                            signal_id: int) -> list[Order]:
        """Close the strategy's open positions matching the signal instrument."""
        with session_scope() as s:
            q = s.query(PositionRow).filter_by(
                mode=mode.value, strategy_id=signal.strategy_id, status="open")
            rows = q.all()
        matches = [r for r in rows
                   if not signal.instrument
                   or signal.instrument in (r.symbol, r.underlying)]
        orders = []
        for row in matches:
            modeled = (signal.reference_price
                       if row.symbol == signal.instrument else None)
            order = self.close_position(row.id, ExitReason.SIGNAL,
                                        modeled_exit_price=modeled,
                                        signal_id=signal_id)
            if order is not None:
                orders.append(order)
        return orders
