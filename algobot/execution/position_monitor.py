"""PositionMonitor: the ~15-second loop that manages every open position.

Central R-management (compendium rule — strategies must NOT reimplement it):

- refresh last_price / unrealized_pnl from quotes;
- stop-loss and take-profit are evaluated on the *underlying* when the
  position carries one (option legs), else on the traded symbol;
- at ``breakeven_at_r`` R of favourable movement the stop moves to entry;
- thereafter an MFE ratchet locks ``ratchet_lock_pct``% of the best
  favourable move (tracked in ``trail_anchor``) — tighten-only, never loosen;
- a stop that fires after being ratcheted past entry reports reason ``trail``.

Exits go through ``order_manager.close_position`` with ``modeled_exit_price``
set to the triggering level, so stop-fire fidelity is measurable.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from algobot.core.config import settings
from algobot.core.enums import ExitReason, ProductType
from algobot.core.models import Position
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import PositionRow

log = logging.getLogger(__name__)

QuoteFn = Callable[[list[str]], dict[str, float]]


def _direction(row: PositionRow) -> int:
    """+1 bullish / -1 bearish management direction for the reference price.

    For option legs (underlying set) the traded side does not indicate the
    view (long puts are bearish), so direction is inferred from where the
    stop sits relative to the underlying entry; TP is the fallback hint.

    Once the stop has been ratcheted it can cross the entry, so the
    comparison anchor is the MFE ``trail_anchor`` (which always stays on the
    favourable side of the stop) when one exists.
    """
    if row.underlying and row.underlying_entry:
        anchor = (row.trail_anchor if row.trail_anchor is not None
                  else row.underlying_entry)
        if row.stop_loss is not None and row.stop_loss != anchor:
            return 1 if row.stop_loss < anchor else -1
        if row.take_profit is not None and row.take_profit != row.underlying_entry:
            return 1 if row.take_profit > row.underlying_entry else -1
    return 1 if row.qty >= 0 else -1


class PositionMonitor:
    """Ticks over open positions: quotes, R-management, SL/TP exits."""

    def __init__(self, order_manager, quote_fn: QuoteFn, risk,
                 on_update: Optional[Callable[[Position, dict[str, float]], None]] = None):
        self.order_manager = order_manager
        self.quote_fn = quote_fn
        self.risk = risk
        self.on_update = on_update
        cfg = settings()["risk"]
        self.breakeven_at_r = float(cfg["breakeven_at_r"])
        self.ratchet_lock_pct = float(cfg["ratchet_lock_pct"])
        init_db()

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        """One monitor pass: refresh marks, manage stops, fire exits."""
        with session_scope() as s:
            rows = s.query(PositionRow).filter_by(status="open").all()
            symbols = {r.symbol for r in rows} | {r.underlying for r in rows
                                                  if r.underlying}
            quotes = self._safe_quotes(sorted(symbols))
            exits: list[tuple[int, ExitReason, float]] = []
            callbacks: list[Position] = []

            for row in rows:
                ltp = quotes.get(row.symbol)
                if ltp is not None:
                    row.last_price = ltp
                    row.unrealized_pnl = round((ltp - row.avg_price) * row.qty, 2)
                ref = quotes.get(row.underlying) if row.underlying else ltp
                if ref is None:
                    continue
                self._manage_stop(row, ref)
                trigger = self._check_triggers(row, ref)
                if trigger is not None:
                    exits.append((row.id, *trigger))
                elif self.on_update is not None:
                    callbacks.append(self._to_model(row))

        # Exits and callbacks run outside the session (they open their own).
        for position_id, reason, level in exits:
            try:
                self.order_manager.close_position(position_id, reason,
                                                  modeled_exit_price=level)
            except Exception:
                log.exception("monitor exit failed for position %d", position_id)
        for pos in callbacks:
            try:
                self.on_update(pos, quotes)
            except Exception:
                log.exception("on_update callback failed for %s", pos.symbol)

    # ------------------------------------------------------------------ internals
    def _safe_quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            return dict(self.quote_fn(symbols) or {})
        except Exception:
            log.exception("monitor quote_fn failed")
            return {}

    def _manage_stop(self, row: PositionRow, ref: float) -> None:
        """Breakeven then MFE-ratchet stop management. Tighten-only."""
        entry = row.underlying_entry if row.underlying else row.avg_price
        if entry is None or row.stop_loss is None:
            return
        d = _direction(row)

        # Track maximum favourable excursion of the reference price.
        anchor = row.trail_anchor if row.trail_anchor is not None else entry
        anchor = max(anchor, ref) if d > 0 else min(anchor, ref)
        row.trail_anchor = anchor
        gain = (anchor - entry) * d
        if gain <= 0:
            return

        stop = row.stop_loss
        # Phase 1: move to breakeven once +breakeven_at_r R in favour.
        if (stop - entry) * d < 0:
            r_dist = abs(entry - stop)
            if r_dist > 0 and gain >= self.breakeven_at_r * r_dist:
                stop = entry
        # Phase 2: once at/past entry, lock ratchet_lock_pct% of the MFE.
        if (stop - entry) * d >= 0:
            locked = entry + d * (self.ratchet_lock_pct / 100.0) * gain
            stop = max(stop, locked) if d > 0 else min(stop, locked)
        # Never loosen.
        if (stop - row.stop_loss) * d > 0:
            log.debug("ratchet %s stop %.2f -> %.2f (anchor %.2f)",
                      row.symbol, row.stop_loss, stop, anchor)
            row.stop_loss = stop

    def _check_triggers(self, row: PositionRow,
                        ref: float) -> Optional[tuple[ExitReason, float]]:
        """Return (reason, modeled_level) when SL or TP is breached."""
        entry = row.underlying_entry if row.underlying else row.avg_price
        d = _direction(row)
        if row.stop_loss is not None:
            breached = ref <= row.stop_loss if d > 0 else ref >= row.stop_loss
            if breached:
                ratcheted = entry is not None and (row.stop_loss - entry) * d >= 0 \
                    and row.trail_anchor is not None
                reason = ExitReason.TRAIL if ratcheted else ExitReason.STOP_LOSS
                return reason, row.stop_loss
        if row.take_profit is not None:
            hit = ref >= row.take_profit if d > 0 else ref <= row.take_profit
            if hit:
                return ExitReason.TAKE_PROFIT, row.take_profit
        return None

    @staticmethod
    def _to_model(row: PositionRow) -> Position:
        from algobot.core.enums import Mode
        return Position(
            strategy_id=row.strategy_id, symbol=row.symbol, qty=row.qty,
            avg_price=row.avg_price, mode=Mode(row.mode), opened_at=row.opened_at,
            stop_loss=row.stop_loss, take_profit=row.take_profit,
            underlying=row.underlying, underlying_entry=row.underlying_entry,
            structure_id=row.structure_id, trail_anchor=row.trail_anchor,
            product_type=ProductType(row.product_type), id=row.id)
