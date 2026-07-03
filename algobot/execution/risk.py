"""Risk engine: per-trade sizing, portfolio caps and the kill switch.

Sizing follows the platform rule: risk a fixed small percentage of capital per
trade, derived from the stop distance, rounded down to whole lots. Caps are
evaluated against the daily ``risk_state`` row which this module owns.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from algobot.core.clock import now_ist, week_start
from algobot.core.config import settings
from algobot.core.enums import Mode, SignalType
from algobot.core.exceptions import RiskRejection
from algobot.core.models import Signal
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import EventLogRow, RiskStateRow, TradeRow

log = logging.getLogger(__name__)


class RiskEngine:
    """Gatekeeper between signals and orders. Config from settings()['risk']."""

    def __init__(self, capital: Optional[float] = None):
        cfg = settings()
        self.capital = float(capital if capital is not None else cfg["capital"])
        self.cfg = dict(cfg["risk"])
        init_db()

    # ------------------------------------------------------------------ sizing
    def size_qty(self, entry: float, stop: float, capital: float, lot: int = 1) -> int:
        """Quantity such that (entry - stop) * qty ~= risk_per_trade_pct% of capital.

        Rounded DOWN to whole lots. Returns 0 for a degenerate stop (zero/negative
        distance) or when even one lot risks more than the budget.
        """
        per_unit_risk = abs(entry - stop)
        if per_unit_risk <= 0 or entry <= 0 or capital <= 0 or lot <= 0:
            return 0
        risk_budget = capital * float(self.cfg["risk_per_trade_pct"]) / 100.0
        units = risk_budget / per_unit_risk
        lots = int(units // lot)
        return lots * lot

    # ------------------------------------------------------------------ checks
    def check(self, signal: Signal, mode: Mode, open_positions_count: int,
              todays) -> None:
        """Raise :class:`RiskRejection` when the signal must not become an order.

        ``todays`` is the current day state (see :meth:`day_state`); pass None
        to have it fetched. EXIT signals always pass — we never block getting
        flat.
        """
        if signal.signal_type == SignalType.EXIT:
            return
        if todays is None:
            todays = self.day_state()

        if bool(getattr(todays, "kill_switch", False)) or self.is_killed():
            raise RiskRejection(
                f"kill switch active ({getattr(todays, 'kill_reason', None) or 'manual'}) "
                f"— no new {mode.value} entries")

        daily_cap = self.capital * float(self.cfg["daily_loss_cap_pct"]) / 100.0
        if todays.realized_day_pnl <= -daily_cap:
            raise RiskRejection(
                f"daily loss cap hit: realized {todays.realized_day_pnl:.0f} "
                f"<= -{daily_cap:.0f}")

        weekly_cap = self.capital * float(self.cfg["weekly_loss_cap_pct"]) / 100.0
        if todays.realized_week_pnl <= -weekly_cap:
            raise RiskRejection(
                f"weekly loss cap hit: realized {todays.realized_week_pnl:.0f} "
                f"<= -{weekly_cap:.0f}")

        if open_positions_count >= int(self.cfg["max_concurrent_positions"]):
            raise RiskRejection(
                f"max concurrent positions reached "
                f"({open_positions_count}/{self.cfg['max_concurrent_positions']})")

        if todays.trades_today >= int(self.cfg["max_trades_per_day"]):
            raise RiskRejection(
                f"global max trades per day reached "
                f"({todays.trades_today}/{self.cfg['max_trades_per_day']})")

    # ------------------------------------------------------------------ day state
    @staticmethod
    def _week_realized(session, today: dt.date) -> float:
        start = dt.datetime.combine(week_start(today), dt.time.min)
        rows = (session.query(TradeRow.net_pnl)
                .filter(TradeRow.exit_time >= start).all())
        return round(sum(r[0] for r in rows), 2)

    def day_state(self) -> RiskStateRow:
        """Today's risk_state row (created on first access, week P&L refreshed)."""
        today = now_ist().date()
        with session_scope() as s:
            row = s.get(RiskStateRow, today)
            if row is None:
                row = RiskStateRow(date=today)
                s.add(row)
            row.realized_week_pnl = self._week_realized(s, today)
            s.flush()
            return row

    def update_day_state(self, pnl_delta: float = 0, trades_delta: int = 0,
                         positions_count: Optional[int] = None) -> RiskStateRow:
        """Upsert today's row: accumulate realized P&L / trade count, refresh week."""
        today = now_ist().date()
        with session_scope() as s:
            row = s.get(RiskStateRow, today)
            if row is None:
                row = RiskStateRow(date=today)
                s.add(row)
            row.realized_day_pnl = round((row.realized_day_pnl or 0.0) + pnl_delta, 2)
            row.trades_today = (row.trades_today or 0) + trades_delta
            if positions_count is not None:
                row.open_position_count = positions_count
            row.realized_week_pnl = self._week_realized(s, today)
            s.flush()
            return row

    # ------------------------------------------------------------------ kill switch
    def set_kill_switch(self, on: bool, reason: str = "") -> None:
        """Flip the kill switch for today and journal the event."""
        today = now_ist().date()
        with session_scope() as s:
            row = s.get(RiskStateRow, today)
            if row is None:
                row = RiskStateRow(date=today)
                s.add(row)
            row.kill_switch = bool(on)
            row.kill_reason = reason or None
            s.add(EventLogRow(
                level="error" if on else "info", source="risk",
                message=f"kill switch {'ON' if on else 'off'}: {reason or 'manual'}"))
        log.warning("kill switch %s (%s)", "ON" if on else "off", reason)

    def is_killed(self) -> bool:
        with session_scope() as s:
            row = s.get(RiskStateRow, now_ist().date())
            return bool(row.kill_switch) if row is not None else False
