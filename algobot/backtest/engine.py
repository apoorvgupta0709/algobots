"""Event-driven bar-replay backtest engine — the reference implementation of
the platform's risk-management OS.

Semantics (mirrored by paper/live execution):
- Strategies are scanned on closed bars per ``meta.scan_schedule``; entries
  fill at the NEXT bar open with segment slippage, costs per leg per side.
- Central risk sizing: ``capital * risk_per_trade_pct%`` per trade from the
  stop distance; whole lots for derivatives; option structures sized so the
  estimated max loss fits the risk budget.
- Central R-management on the UNDERLYING: stop to entry at +``breakeven_at_r``R,
  then an MFE ratchet locking ``ratchet_lock_pct``% of the best favourable
  move (never lowered); ratcheted exits are tagged TRAIL.
- Intraday square-off at/after 15:15 IST; options settle at intrinsic on
  expiry close; multi-leg structures aggregate into one trade row.
"""
from __future__ import annotations

import copy
import datetime as dt
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from functools import reduce
from typing import Optional

import numpy as np
import pandas as pd

from algobot.backtest import compat, fills
from algobot.backtest.metrics import compute_metrics
from algobot.backtest.option_data import OptionDataProvider
from algobot.backtest.portfolio import BookEntry, Leg, PositionBook
from algobot.core.clock import FIRST_SCAN, IST, SQUAREOFF_START
from algobot.core.config import settings
from algobot.core.enums import ExitReason, OptionType, Side, SignalType, Timeframe
from algobot.core.models import OptionStructure, Position, Signal, Trade
from algobot.core.strategy import (SCAN_0920_ONCE, SCAN_EOD, SCAN_EVERY_5MIN,
                                   SCAN_EVERY_15MIN, SCAN_EXPIRY_DAY,
                                   SCAN_MONTHLY, SCAN_WEEKLY, StrategyBase,
                                   StrategyContext)
from algobot.core.universes import lot_size
from algobot.costs.india import CostModel

log = logging.getLogger(__name__)

LAST_SCAN = dt.time(15, 10)          # no fresh intraday entries after this
PENDING_TTL_BARS = 5                 # drop unfillable pending orders after N bars


@dataclass
class BacktestResult:
    """Output of one engine run; ``persist()`` stores it via the report module."""
    trades: list[Trade]
    equity: pd.Series
    metrics: dict
    data_source: str
    strategy_id: str = ""
    params: dict = field(default_factory=dict)
    start: Optional[dt.date] = None
    end: Optional[dt.date] = None
    open_positions: list[Position] = field(default_factory=list)

    def persist(self) -> int:
        from algobot.backtest.report import persist_run
        return persist_run(self.strategy_id, self.params, self.start, self.end,
                           self.data_source, self.metrics, self.trades)


@dataclass
class _Pending:
    signal: Signal
    structure: Optional[OptionStructure]
    created_step: int


class BacktestEngine:
    """Bar-replay engine for one strategy over pre-fetched candle frames."""

    def __init__(self, strategy: StrategyBase, data: dict[str, pd.DataFrame],
                 capital: float = 100_000,
                 cost_model: CostModel | None = None,
                 option_data: "OptionDataProvider | None" = None):
        self.strategy = strategy
        self.meta = strategy.meta
        self.capital = float(capital)
        self.cost_model = cost_model or CostModel()
        self.data = {sym: self._prep(df) for sym, df in data.items() if len(df)}
        if not self.data:
            raise ValueError("BacktestEngine needs at least one non-empty frame")
        self.provider = option_data or OptionDataProvider(iv_source=self._iv_for_option)
        risk = settings()["risk"]
        self.risk_pct = float(risk["risk_per_trade_pct"])
        self.be_r = float(risk["breakeven_at_r"])
        self.lock_pct = float(risk["ratchet_lock_pct"])
        self.book = PositionBook(self.capital)
        self._builder = compat.FallbackLegBuilder(iv_source=self._iv_estimate)
        self._leg_builder = self._make_leg_builder()
        self._chain_cls = self._import_chain_cls()
        self._last_close: dict[str, float] = {}
        self._iv_cache: dict[tuple[str, dt.date], float] = {}
        self._day_last: dict[dt.date, pd.Timestamp] = {}

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _prep(df: pd.DataFrame) -> pd.DataFrame:
        df = df.sort_index()
        df.columns = [str(c).lower() for c in df.columns]
        if df.index.tz is None:
            df = df.tz_localize(IST)
        return df

    @staticmethod
    def _make_leg_builder():
        try:
            from algobot.options.leg_builder import LegBuilder  # lazy: real first
            return LegBuilder()
        except Exception:
            return None

    @staticmethod
    def _import_chain_cls():
        try:
            from algobot.options.chain import OptionChain  # lazy: real first
            return OptionChain
        except Exception:
            return None

    # ------------------------------------------------------------------ run
    def run(self) -> BacktestResult:
        union = reduce(lambda a, b: a.union(b), (df.index for df in self.data.values()))
        pos_of = {sym: df.index.searchsorted(union, side="right")
                  for sym, df in self.data.items()}
        self._build_calendar(union)
        window = max(self.meta.warmup_bars + 10, 400)

        pending: list[_Pending] = []
        pending_exits: list[tuple[str, ExitReason]] = []
        entries_today: dict[dt.date, int] = defaultdict(int)
        equity_vals: list[float] = []

        log.info("Backtesting %s over %d bars (%s .. %s), capital %.0f",
                 self.meta.strategy_id, len(union), union[0], union[-1], self.capital)

        for step, ts in enumerate(union):
            bars: dict[str, pd.Series] = {}
            for sym, df in self.data.items():
                p = pos_of[sym][step]
                if p > 0 and df.index[p - 1] == ts:
                    row = df.iloc[p - 1]
                    bars[sym] = row
                    self._last_close[sym] = float(row["close"])

            pending_exits = self._process_exit_orders(pending_exits, ts, bars)
            pending = [pe for pe in pending
                       if not self._try_fill(pe, step, ts, bars)]
            self._manage_positions(ts, bars)
            equity_vals.append(self.book.equity(
                lambda entry, leg: self._mark(entry, leg, ts)))

            if not self._is_scan_bar(ts) or not self._warmed(pos_of, step):
                continue
            data_slice = {sym: self.data[sym].iloc[max(0, pos_of[sym][step] - window):
                                                   pos_of[sym][step]]
                          for sym in self.data if pos_of[sym][step] > 0}
            ctx = StrategyContext(
                now=ts.to_pydatetime(),
                open_positions=self.book.positions(),
                capital_allocated=self.capital,
                option_chain=self._chain_factory(ts),
                leg_builder=self._leg_builder or self._builder,
                trades_today=entries_today[ts.date()])
            for sig in (self.strategy.generate_signals(data_slice, ctx) or []):
                self._route(sig, ts, step, pending, pending_exits, entries_today)

        equity = pd.Series(equity_vals, index=union, name="equity", dtype=float)
        result = BacktestResult(
            trades=list(self.book.trades), equity=equity,
            metrics=compute_metrics(self.book.trades, equity),
            data_source=self.provider.data_source,
            strategy_id=self.meta.strategy_id, params=dict(self.strategy.params),
            start=union[0].date(), end=union[-1].date(),
            open_positions=self.book.positions())
        log.info("Done %s: %d trades, net %.0f, data_source=%s",
                 self.meta.strategy_id, len(result.trades),
                 result.metrics["net_pnl"], result.data_source)
        return result

    def _warmed(self, pos_of: dict, step: int) -> bool:
        return max(pos[step] for pos in pos_of.values()) >= self.meta.warmup_bars

    # ------------------------------------------------------------------ scheduling
    def _build_calendar(self, union: pd.DatetimeIndex) -> None:
        self._day_last = {}
        day_first_scan: dict[dt.date, pd.Timestamp] = {}
        day_first: dict[dt.date, pd.Timestamp] = {}
        for ts in union:
            d = ts.date()
            day_first.setdefault(d, ts)
            self._day_last[d] = ts
            if ts.time() >= FIRST_SCAN:
                day_first_scan.setdefault(d, ts)
        self._day_first_scan = {d: day_first_scan.get(d, day_first[d])
                                for d in day_first}
        self._week_last: dict[tuple, pd.Timestamp] = {}
        self._month_first_day: dict[tuple, dt.date] = {}
        for d in sorted(self._day_last):
            iso = d.isocalendar()
            self._week_last[(iso[0], iso[1])] = self._day_last[d]
            self._month_first_day.setdefault((d.year, d.month), d)

    def _is_scan_bar(self, ts: pd.Timestamp) -> bool:
        token, t, d = self.meta.scan_schedule, ts.time(), ts.date()
        if token in (SCAN_EVERY_5MIN, SCAN_EVERY_15MIN):
            return FIRST_SCAN <= t <= LAST_SCAN
        if token == SCAN_0920_ONCE:
            return ts == self._day_first_scan[d]
        if token == SCAN_EOD:
            return ts == self._day_last[d]
        if token == SCAN_WEEKLY:
            iso = d.isocalendar()
            return ts == self._week_last[(iso[0], iso[1])]
        if token == SCAN_MONTHLY:
            return (d == self._month_first_day[(d.year, d.month)]
                    and ts == self._day_first_scan[d])
        if token == SCAN_EXPIRY_DAY:
            return FIRST_SCAN <= t <= LAST_SCAN and self._is_expiry_day(d)
        log.warning("Unknown scan schedule %r — never scanning", token)
        return False

    def _is_expiry_day(self, day: dt.date) -> bool:
        root = compat.root_of(next(iter(self.data)))
        try:
            from algobot.data.expiries import is_expiry_day  # lazy
            return bool(is_expiry_day(root, day))
        except Exception:
            return compat.is_expiry_day_fallback(root, day)

    # ------------------------------------------------------------------ signal routing
    def _route(self, sig: Signal, ts: pd.Timestamp, step: int,
               pending: list[_Pending],
               pending_exits: list[tuple[str, ExitReason]],
               entries_today: dict[dt.date, int]) -> None:
        st = sig.signal_type
        if st == SignalType.EXIT:
            for entry in self.book.entries_for(sig.instrument):
                pending_exits.append((entry.entry_id, ExitReason.SIGNAL))
            return
        if st == SignalType.ADJUST:
            log.warning("ADJUST signals are not supported in backtest — ignoring")
            return
        if st not in (SignalType.ENTRY_LONG, SignalType.ENTRY_SHORT,
                      SignalType.REBALANCE):
            return
        if sig.pair_leg is not None:
            log.warning("pair_leg on %s not supported — trading primary leg only",
                        sig.instrument)
        is_risk = st != SignalType.REBALANCE
        if is_risk:
            if entries_today[ts.date()] >= self.meta.max_trades_per_day:
                log.debug("max_trades_per_day hit — dropping %s", sig.instrument)
                return
            pending_risk = sum(1 for pe in pending
                               if pe.signal.signal_type != SignalType.REBALANCE)
            if self.book.risk_entry_count() + pending_risk >= self.meta.max_positions:
                log.debug("max_positions hit — dropping %s", sig.instrument)
                return
        structure = None
        if sig.structure is not None:
            structure = self._resolve_structure(copy.deepcopy(sig.structure),
                                                sig.reference_price, ts)
            if structure is None:
                return
        pending.append(_Pending(sig, structure, step))
        if is_risk:
            entries_today[ts.date()] += 1

    def _resolve_structure(self, structure: OptionStructure, spot: float,
                           ts: pd.Timestamp) -> Optional[OptionStructure]:
        now = ts.to_pydatetime()
        if self._leg_builder is not None:
            for meth in ("resolve", "build", "resolve_structure"):
                fn = getattr(self._leg_builder, meth, None)
                if fn is None:
                    continue
                try:
                    out = fn(structure, spot, now)
                    structure = out or structure
                    break
                except Exception:
                    log.debug("real LegBuilder.%s failed — using fallback", meth,
                              exc_info=True)
        if any(l.resolved_symbol is None for l in structure.legs):
            structure = self._builder.resolve(structure, spot, now)
        if any(l.resolved_symbol is None for l in structure.legs):
            log.warning("Could not resolve legs for %s — dropping signal",
                        structure.name)
            return None
        return structure

    # ------------------------------------------------------------------ entry fills
    def _open_ts(self, ts: pd.Timestamp) -> pd.Timestamp:
        """Timestamp of the bar's OPEN: 09:15 IST for daily bars, else the label.

        Open-based fills on daily data must be priced at the session open —
        otherwise options entered on their expiry date would be (mis)priced at
        the 15:30 settlement moment.
        """
        if self.meta.timeframe == Timeframe.DAY:
            return ts.normalize() + pd.Timedelta(hours=9, minutes=15)
        return ts

    def _try_fill(self, pe: _Pending, step: int, ts: pd.Timestamp,
                  bars: dict[str, pd.Series]) -> bool:
        """Fill (or expire) a pending order at this bar's open. True = consumed."""
        if step == pe.created_step:
            return False                                   # fills at NEXT bar
        bar = bars.get(pe.signal.instrument)
        if bar is None:
            return step - pe.created_step > PENDING_TTL_BARS
        sig = pe.signal
        fill_ts = self._open_ts(ts)
        if pe.structure is not None:
            expiries = [dt.date.fromisoformat(l.resolved_expiry)
                        for l in pe.structure.legs if l.resolved_expiry]
            if expiries and min(expiries) < fill_ts.date():
                log.warning("Legs of %s expired before the fill bar — dropping",
                            pe.structure.name)
                return True
        o = float(bar["open"])
        direction = -1 if sig.signal_type == SignalType.ENTRY_SHORT else 1
        risk_amt = fills.risk_amount(self.capital, self.risk_pct)
        legs = (self._structure_legs(pe, fill_ts, o, direction, risk_amt)
                if pe.structure is not None
                else self._cash_leg(sig, o, direction, risk_amt))
        if not legs:
            return True
        self.book.open(BookEntry(
            strategy_id=self.strategy.strategy_id, instrument=sig.instrument,
            direction=direction, legs=legs, entry_time=fill_ts,
            underlying_entry=o, product_type=sig.product_type,
            stop=sig.stop_loss, target=sig.take_profit,
            initial_stop=sig.stop_loss, structure=pe.structure,
            is_risk_trade=sig.signal_type != SignalType.REBALANCE))
        return True

    def _cash_leg(self, sig: Signal, o: float, direction: int,
                  risk_amt: float) -> list[Leg]:
        sym = sig.instrument
        if sig.size_hint is not None:
            qty = fills.qty_from_hint(sig.size_hint, o, self.capital)
            if o > 0:                       # accumulation cannot exceed free cash
                qty = min(qty, int(max(self.book.cash, 0.0) // o))
        else:
            qty = fills.cash_qty(risk_amt, o, sig.stop_loss, self.capital)
        if qty <= 0:
            return []
        side = Side.BUY if direction > 0 else Side.SELL
        px = fills.fill_price(self.cost_model, sym, side, o, sig.product_type)
        cost = fills.order_cost(self.cost_model, sym, side, qty, px, sig.product_type)
        return [Leg(symbol=sym, qty=direction * qty, entry_price=px, entry_cost=cost)]

    def _structure_legs(self, pe: _Pending, ts: pd.Timestamp, o: float,
                        direction: int, risk_amt: float) -> list[Leg]:
        sig, structure = pe.signal, pe.structure
        lot = lot_size(structure.underlying)
        premiums = [self.provider.premium(l.resolved_symbol, ts, spot=o)
                    for l in structure.legs]
        unit_net = sum(int(l.side) * px * l.lots
                       for l, px in zip(structure.legs, premiums))
        all_fut = all(l.option_type == OptionType.FUT for l in structure.legs)
        if sig.size_hint is not None:
            per_lot = abs(unit_net) * lot if not all_fut else o * lot
            lots = fills.lots_from_hint(sig.size_hint, per_lot, self.capital)
        elif all_fut:
            lots = fills.derivative_lots(risk_amt, o, sig.stop_loss, lot)
        elif structure.net_direction == "credit" or unit_net < 0:
            lots = fills.credit_lots(structure, o, lot, risk_amt, self.capital)
        else:
            lots = fills.debit_lots(risk_amt, unit_net * lot, self.capital)
        if lots <= 0:
            log.warning("Sized to zero lots for %s — dropping entry", structure.name)
            return []
        legs: list[Leg] = []
        for leg_def, base in zip(structure.legs, premiums):
            qty = int(leg_def.side) * lots * leg_def.lots * lot
            side = Side.BUY if qty > 0 else Side.SELL
            px = fills.fill_price(self.cost_model, leg_def.resolved_symbol, side,
                                  base, sig.product_type)
            cost = fills.order_cost(self.cost_model, leg_def.resolved_symbol, side,
                                    qty, px, sig.product_type)
            legs.append(Leg(symbol=leg_def.resolved_symbol, qty=qty,
                            entry_price=px, entry_cost=cost,
                            option_type=leg_def.option_type.value,
                            expiry=dt.date.fromisoformat(leg_def.resolved_expiry)
                            if leg_def.resolved_expiry else None))
        return legs

    # ------------------------------------------------------------------ exits
    def _process_exit_orders(self, pending_exits: list[tuple[str, ExitReason]],
                             ts: pd.Timestamp,
                             bars: dict[str, pd.Series]) -> list[tuple[str, ExitReason]]:
        keep: list[tuple[str, ExitReason]] = []
        by_id = {e.entry_id: e for e in self.book.entries}
        for entry_id, reason in pending_exits:
            entry = by_id.get(entry_id)
            if entry is None:
                continue                                   # already closed
            bar = bars.get(entry.instrument)
            if bar is None:
                keep.append((entry_id, reason))
                continue
            o = float(bar["open"])
            self._close_entry(entry, self._open_ts(ts), exit_underlying=o,
                              reason=reason, modeled=o)
        return keep

    def _manage_positions(self, ts: pd.Timestamp, bars: dict[str, pd.Series]) -> None:
        t = ts.time()
        for entry in list(self.book.entries):
            bar = bars.get(entry.instrument)
            if self._settle_if_expired(entry, ts, bar):
                continue
            if bar is None:
                continue
            o, h, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
            if self.meta.intraday_squareoff and t >= SQUAREOFF_START:
                self._close_entry(entry, ts, o, ExitReason.SQUAREOFF, modeled=o)
                continue
            d = entry.direction
            if entry.stop is not None:
                hit = low <= entry.stop if d > 0 else h >= entry.stop
                if hit:                                    # SL before TP: conservative
                    exit_und = min(o, entry.stop) if d > 0 else max(o, entry.stop)
                    reason = (ExitReason.TRAIL if entry.stop_ratcheted
                              else ExitReason.STOP_LOSS)
                    self._close_entry(entry, ts, exit_und, reason, modeled=entry.stop)
                    continue
            if entry.target is not None:
                hit = h >= entry.target if d > 0 else low <= entry.target
                if hit:
                    exit_und = max(o, entry.target) if d > 0 else min(o, entry.target)
                    self._close_entry(entry, ts, exit_und, ExitReason.TAKE_PROFIT,
                                      modeled=entry.target)
                    continue
            self._update_trail(entry, h, low)

    def _update_trail(self, entry: BookEntry, high: float, low: float) -> None:
        """Central R-management: breakeven at +be_r R, then the MFE ratchet."""
        if entry.initial_stop is None or not entry.is_risk_trade:
            return
        d, e = entry.direction, entry.underlying_entry
        r = abs(e - entry.initial_stop)
        if r <= 0:
            return
        fav_px = high if d > 0 else low
        if entry.trail_anchor is None or (fav_px - entry.trail_anchor) * d > 0:
            entry.trail_anchor = fav_px
        fav = (entry.trail_anchor - e) * d
        if not entry.be_done and fav >= self.be_r * r:
            entry.be_done = True                 # ratchet engages only after this
        if not entry.be_done:
            return
        locked = e + d * fav * self.lock_pct / 100.0
        candidate = max(e, locked) if d > 0 else min(e, locked)
        if entry.stop is None or (candidate - entry.stop) * d > 0:   # never lowered
            entry.stop = candidate
            entry.stop_ratcheted = True

    def _settle_if_expired(self, entry: BookEntry, ts: pd.Timestamp,
                           bar: Optional[pd.Series]) -> bool:
        expiries = entry.expiries()
        if not expiries:
            return False
        first = min(expiries)
        d = ts.date()
        due = d > first or (d == first and ts == self._day_last.get(d))
        if not due:
            return False
        spot = (float(bar["close"]) if bar is not None
                else self._last_close.get(entry.instrument, entry.underlying_entry))
        prices, costs = {}, {}
        for leg in entry.legs:
            prices[leg.symbol] = self._intrinsic(leg, spot)
            side = Side.SELL if leg.qty > 0 else Side.BUY
            costs[leg.symbol] = fills.order_cost(
                self.cost_model, leg.symbol, side, leg.qty,
                prices[leg.symbol], entry.product_type)
        self.book.close(entry, ts, prices, costs, ExitReason.EXPIRY,
                        modeled_exit=spot)
        return True

    @staticmethod
    def _intrinsic(leg: Leg, spot: float) -> float:
        if leg.option_type == OptionType.FUT.value:
            return round(spot, 2)
        from algobot.backtest.option_data import _parse
        parsed = _parse(leg.symbol)
        if parsed is None:
            return round(spot, 2)
        _root, _expiry, strike, opt = parsed
        value = max(spot - strike, 0.0) if opt == "CE" else max(strike - spot, 0.0)
        return round(value, 2)

    def _close_entry(self, entry: BookEntry, ts: pd.Timestamp,
                     exit_underlying: float, reason: ExitReason,
                     modeled: Optional[float]) -> None:
        """Close all legs: underlying-priced exits, option legs via the provider."""
        prices, costs = {}, {}
        for leg in entry.legs:
            raw = (exit_underlying if leg.option_type == "CASH"
                   else self.provider.premium(leg.symbol, ts, spot=exit_underlying))
            side = Side.SELL if leg.qty > 0 else Side.BUY
            px = fills.fill_price(self.cost_model, leg.symbol, side, raw,
                                  entry.product_type)
            prices[leg.symbol] = px
            costs[leg.symbol] = fills.order_cost(self.cost_model, leg.symbol, side,
                                                 leg.qty, px, entry.product_type)
        self.book.close(entry, ts, prices, costs, reason, modeled_exit=modeled)

    # ------------------------------------------------------------------ marking / iv
    def _mark(self, entry: BookEntry, leg: Leg, ts: pd.Timestamp) -> float:
        if leg.option_type == "CASH":
            return self._last_close.get(leg.symbol, leg.entry_price)
        spot = self._last_close.get(entry.instrument, entry.underlying_entry)
        return self.provider.premium(leg.symbol, ts, spot=spot)

    def _chain_factory(self, ts: pd.Timestamp):
        def get_chain(underlying: str):
            spot = self._last_close.get(underlying)
            if spot is None:
                for sym in self.data:
                    if compat.root_of(sym) == compat.root_of(underlying):
                        spot = self._last_close.get(sym)
                        break
            spot = spot or 0.0
            iv = self._iv_estimate(underlying, ts.to_pydatetime())
            if self._chain_cls is not None:
                try:
                    return self._chain_cls.synthetic(underlying=underlying,
                                                     spot=spot, iv=iv,
                                                     now=ts.to_pydatetime())
                except Exception:
                    log.debug("real OptionChain.synthetic failed — using fallback",
                              exc_info=True)
            return compat.SyntheticOptionChain(underlying, spot, iv,
                                               ts.to_pydatetime())
        return get_chain

    def _iv_estimate(self, underlying: str, now: dt.datetime) -> float:
        """Annualised realised vol of the last 20 daily closes (fallback 0.14)."""
        df = self.data.get(underlying)
        if df is None:
            for sym in self.data:
                if compat.root_of(sym) == compat.root_of(underlying):
                    df = self.data[sym]
                    break
        if df is None:
            return compat.DEFAULT_IV
        key = (underlying, now.date())
        if key in self._iv_cache:
            return self._iv_cache[key]
        closes = df["close"][df.index <= pd.Timestamp(now)]
        step = df.index[1] - df.index[0] if len(df) > 1 else pd.Timedelta(days=1)
        if step < pd.Timedelta(days=1):
            closes = closes.resample("1D").last().dropna()
        closes = closes.tail(21)
        iv = compat.DEFAULT_IV
        if len(closes) >= 5:
            log_rets = np.diff(np.log(closes.to_numpy(dtype=float)))
            rv = float(np.std(log_rets, ddof=1) * math.sqrt(252))
            if math.isfinite(rv) and rv > 0:
                iv = min(max(rv, 0.05), 1.5)
        self._iv_cache[key] = iv
        return iv

    def _iv_for_option(self, symbol: str, now: dt.datetime) -> float:
        """IV source for the default provider: match the option root to a frame."""
        target = symbol.split(":")[-1].upper()
        for sym in self.data:
            root = compat.root_of(sym)
            if target.startswith(root):
                return self._iv_estimate(sym, now)
        return compat.DEFAULT_IV
