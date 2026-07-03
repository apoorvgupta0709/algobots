"""StrategyRunner: scan/monitor/snapshot loops over the active strategy set.

Resilience first — this loop runs unattended with real money. Every
per-strategy step (data fetch, signal generation, order submission, monitor
hook) is individually guarded: one strategy's failure is journalled to
event_log and never breaks the others.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Callable, Optional

import pandas as pd

from algobot.core import universes
from algobot.core.clock import now_ist
from algobot.core.enums import Mode, ProductType, Timeframe
from algobot.core.exceptions import DataError
from algobot.core.models import Position, Signal
from algobot.core.strategy import StrategyBase, StrategyContext
from algobot.data.feed import DataFeed
from algobot.engine import lifecycle
from algobot.execution.position_monitor import PositionMonitor
from algobot.options.chain import OptionChain
from algobot.options.leg_builder import LegBuilder
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import (
    EquitySnapshotRow,
    EventLogRow,
    PositionRow,
    StrategyRow,
    TradeRow,
)

log = logging.getLogger(__name__)

#: Minutes in a full NSE cash session (09:15-15:30).
SESSION_MINUTES = 375
#: Extra trading days of history beyond the strict warmup requirement.
WARMUP_BUFFER_DAYS = 2
#: Minimum calendar lookback for daily-timeframe strategies.
DAILY_MIN_LOOKBACK_DAYS = 400
#: Strikes each side of ATM to quote when building a live option chain.
CHAIN_STRIKES_EACH_SIDE = 10


def _naive(ts: dt.datetime) -> dt.datetime:
    return ts.replace(tzinfo=None) if ts.tzinfo else ts


def _to_position(row: PositionRow) -> Position:
    return Position(
        strategy_id=row.strategy_id, symbol=row.symbol, qty=row.qty,
        avg_price=row.avg_price, mode=Mode(row.mode), opened_at=row.opened_at,
        stop_loss=row.stop_loss, take_profit=row.take_profit,
        underlying=row.underlying, underlying_entry=row.underlying_entry,
        structure_id=row.structure_id, trail_anchor=row.trail_anchor,
        product_type=ProductType(row.product_type), id=row.id)


class StrategyRunner:
    """Drives active strategies: scheduled scans, the monitor loop and
    equity snapshots. Constructed once by the scheduler service."""

    def __init__(self, feed: DataFeed, order_manager, risk) -> None:
        self.feed = feed
        self.order_manager = order_manager
        self.risk = risk
        self.monitor = PositionMonitor(order_manager, self._safe_quotes, risk,
                                       on_update=self._dispatch_position_hook)
        init_db()

    # ------------------------------------------------------------------ scan
    def scan(self, schedule_token: str, now: Optional[dt.datetime] = None) -> None:
        """Run every active strategy whose ``meta.scan_schedule`` matches.

        Each strategy is fully isolated: any exception is journalled and the
        loop moves on to the next one.
        """
        now = now or now_ist()
        active = lifecycle.get_active()
        due = [(strat, row) for strat, row in active
               if strat.meta.scan_schedule == schedule_token]
        if not due:
            log.debug("scan %s: no active strategies due", schedule_token)
            return
        log.info("scan %s: %d strategies due", schedule_token, len(due))
        for strategy, row in due:
            try:
                self._scan_one(strategy, row, now)
            except Exception as exc:
                log.exception("scan failed for %s", strategy.strategy_id)
                self._journal("error",
                              f"scan failed for {strategy.strategy_id}: {exc}",
                              {"strategy_id": strategy.strategy_id,
                               "schedule": schedule_token})

    def _scan_one(self, strategy: StrategyBase, row: StrategyRow,
                  now: dt.datetime) -> None:
        mode = Mode(row.mode)
        ctx = self._build_context(strategy.strategy_id, row, now)
        symbols = universes.resolve(strategy.universe(ctx))
        if not symbols:
            return

        start, end = self._candle_window(strategy.meta, now)
        data: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            try:
                df = self.feed.get_candles(symbol, strategy.meta.timeframe,
                                           start, end)
                if df is not None and not df.empty:
                    data[symbol] = df
            except DataError as exc:
                log.warning("no candles for %s (%s): %s",
                            symbol, strategy.strategy_id, exc)
            except Exception as exc:
                log.exception("candle fetch failed for %s (%s)",
                              symbol, strategy.strategy_id)
                self._journal("warn",
                              f"candle fetch failed for {symbol} "
                              f"({strategy.strategy_id}): {exc}")
        if not data:
            self._journal("warn",
                          f"scan skipped for {strategy.strategy_id}: no data "
                          f"for any of {symbols[:5]}",
                          {"strategy_id": strategy.strategy_id})
            return

        ctx.option_chain = self._chain_provider(data, now)
        ctx.leg_builder = LegBuilder(chain_provider=ctx.option_chain)

        try:
            signals = strategy.generate_signals(data, ctx) or []
        except Exception as exc:
            log.exception("generate_signals failed for %s", strategy.strategy_id)
            self._journal("error",
                          f"generate_signals failed for "
                          f"{strategy.strategy_id}: {exc}",
                          {"strategy_id": strategy.strategy_id})
            return

        for signal in signals:
            self._submit(signal, mode, row.capital_alloc)

    def _submit(self, signal: Signal, mode: Mode, capital: float) -> None:
        try:
            orders = self.order_manager.submit(signal, mode=mode, capital=capital)
            log.info("signal %s %s (%s) -> %d order(s)",
                     signal.strategy_id, signal.signal_type.value,
                     signal.instrument, len(orders))
        except Exception as exc:
            log.exception("submit failed for %s", signal.strategy_id)
            self._journal("error",
                          f"order submit failed for {signal.strategy_id} "
                          f"({signal.instrument}): {exc}",
                          {"strategy_id": signal.strategy_id})

    # ------------------------------------------------------------------ window
    @staticmethod
    def _candle_window(meta, now: dt.datetime) -> tuple[dt.date, dt.date]:
        """Calendar (start, end) covering warmup_bars of history plus buffer."""
        end = now.date()
        if meta.timeframe == Timeframe.DAY:
            lookback = max(DAILY_MIN_LOOKBACK_DAYS, meta.warmup_bars * 2)
            return end - dt.timedelta(days=lookback), end
        tf_min = int(meta.timeframe.value)
        trading_days = math.ceil(meta.warmup_bars * tf_min / SESSION_MINUTES) \
            + WARMUP_BUFFER_DAYS
        # Roughly double for weekends/holidays, plus a small constant pad.
        return end - dt.timedelta(days=trading_days * 2 + 5), end

    # ------------------------------------------------------------------ context
    def _build_context(self, strategy_id: str, row: StrategyRow,
                       now: dt.datetime) -> StrategyContext:
        day_start = dt.datetime.combine(now.date(), dt.time.min)
        with session_scope() as s:
            pos_rows = (s.query(PositionRow)
                        .filter_by(strategy_id=strategy_id, mode=row.mode,
                                   status="open").all())
            trades_today = (s.query(TradeRow)
                            .filter(TradeRow.strategy_id == strategy_id,
                                    TradeRow.mode == row.mode,
                                    TradeRow.exit_time >= day_start)
                            .count())
        return StrategyContext(
            now=now,
            open_positions=[_to_position(r) for r in pos_rows],
            capital_allocated=float(row.capital_alloc),
            trades_today=trades_today)

    # ------------------------------------------------------------------ chains
    def _chain_provider(self, data: dict[str, pd.DataFrame],
                        now: dt.datetime) -> Callable[[str], OptionChain]:
        """underlying -> OptionChain: quote-backed when live quotes are
        retrievable, else synthetic seeded with the last close."""

        def provider(underlying: str) -> OptionChain:
            live_spot = self._safe_quotes([underlying]).get(underlying)
            df = data.get(underlying)
            last_close = (float(df["close"].iloc[-1])
                          if df is not None and not df.empty else None)
            spot = live_spot or last_close
            if spot is None or spot <= 0:
                raise DataError(f"no spot price available for {underlying}")
            if live_spot is not None:
                quotes = self._option_quotes(underlying, spot, now)
                if quotes:
                    return OptionChain.from_quotes(underlying, spot, now, quotes)
            return OptionChain.synthetic(underlying, spot, now)

        return provider

    def _option_quotes(self, underlying: str, spot: float,
                       now: dt.datetime) -> dict[str, float]:
        """Best-effort live premiums for ATM +/- N strikes, nearest expiry."""
        try:
            from algobot.data.expiries import next_expiry
            from algobot.data.instruments import option_symbol, root_of

            root = root_of(underlying)
            expiry = next_expiry(root, "weekly", 0, on_date=now.date())
            step = universes.strike_step(underlying)
            atm = round(spot / step) * step
            symbols = [option_symbol(root, expiry, atm + i * step, ot)
                       for i in range(-CHAIN_STRIKES_EACH_SIDE,
                                      CHAIN_STRIKES_EACH_SIDE + 1)
                       for ot in ("CE", "PE")]
            return self._safe_quotes(symbols)
        except Exception:
            log.debug("option quote fetch failed for %s; synthetic chain",
                      underlying, exc_info=True)
            return {}

    # ------------------------------------------------------------------ monitor
    def monitor_tick(self) -> None:
        """One PositionMonitor pass. Strategy ``on_position_update`` hooks are
        dispatched from the monitor's on_update callback (only for strategies
        that override the hook)."""
        try:
            self.monitor.tick()
        except Exception as exc:
            log.exception("monitor tick failed")
            self._journal("error", f"monitor tick failed: {exc}")

    def _dispatch_position_hook(self, position: Position,
                                ltp_map: dict[str, float]) -> None:
        """Route a live position update to its strategy's hook; submit any
        returned signals. Guarded per position."""
        try:
            from algobot.core import registry
            cls = registry.all_strategies().get(position.strategy_id)
            if cls is None:
                return
            # Skip the (common) default no-op hook without touching the DB.
            if cls.on_position_update is StrategyBase.on_position_update:
                return
            with session_scope() as s:
                row = s.get(StrategyRow, position.strategy_id)
            if row is None or not row.enabled:
                return
            strategy = cls(row.params_json or {})
            ctx = self._build_context(position.strategy_id, row, now_ist())
            signals = strategy.on_position_update(position, dict(ltp_map), ctx) or []
            for signal in signals:
                self._submit(signal, Mode(row.mode), row.capital_alloc)
        except Exception as exc:
            log.exception("on_position_update hook failed for %s",
                          position.strategy_id)
            self._journal("error",
                          f"on_position_update failed for "
                          f"{position.strategy_id}: {exc}")

    # ------------------------------------------------------------------ snapshots
    def snapshot_equity(self, now: Optional[dt.datetime] = None) -> int:
        """Write one EquitySnapshotRow per active strategy.

        equity = capital_alloc + today's realized (TradeRows) + unrealized
        (open PositionRow marks). Returns the number of rows written.
        """
        now = now or now_ist()
        ts = _naive(now)
        day_start = dt.datetime.combine(now.date(), dt.time.min)
        written = 0
        for strategy, row in lifecycle.get_active():
            try:
                with session_scope() as s:
                    realized = (s.query(TradeRow)
                                .filter(TradeRow.strategy_id == row.strategy_id,
                                        TradeRow.mode == row.mode,
                                        TradeRow.exit_time >= day_start)
                                .all())
                    realized_pnl = sum(float(t.net_pnl) for t in realized)
                    open_rows = (s.query(PositionRow)
                                 .filter_by(strategy_id=row.strategy_id,
                                            mode=row.mode, status="open")
                                 .all())
                    unrealized_pnl = sum(float(p.unrealized_pnl or 0.0)
                                         for p in open_rows)
                    day_pnl = round(realized_pnl + unrealized_pnl, 2)
                    s.add(EquitySnapshotRow(
                        ts=ts, strategy_id=row.strategy_id, mode=row.mode,
                        equity=round(float(row.capital_alloc) + day_pnl, 2),
                        day_pnl=day_pnl))
                written += 1
            except Exception as exc:
                log.exception("equity snapshot failed for %s", row.strategy_id)
                self._journal("error",
                              f"equity snapshot failed for {row.strategy_id}: {exc}")
        return written

    # ------------------------------------------------------------------ plumbing
    def _safe_quotes(self, symbols: list[str]) -> dict[str, float]:
        """feed.get_quotes that never raises (paper/offline safe)."""
        if not symbols:
            return {}
        try:
            return dict(self.feed.get_quotes(list(symbols)) or {})
        except Exception:
            log.debug("quote fetch failed for %d symbols", len(symbols),
                      exc_info=True)
            return {}

    @staticmethod
    def _journal(level: str, message: str, detail: dict | None = None) -> None:
        try:
            with session_scope() as s:
                s.add(EventLogRow(level=level, source="engine", message=message,
                                  detail_json=detail))
        except Exception:
            log.exception("failed to journal: %s", message)
