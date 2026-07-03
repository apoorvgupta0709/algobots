"""3.8 Momentum Factor (12-month formation) — cross-sectional momentum sleeve.

Buy what has been strong: rank the universe by 12-month return excluding the
latest month (12-1), hold the top names equal-weight, rebalance monthly. The
edge is the momentum premium — the highest-premium factor documented in Indian
equities historically. Regime: trending bull markets; momentum compounds when
leadership persists. Primary risk: violent crash reversals (March 2020) —
momentum falls hardest when regimes flip, hence the 200-day composite crash
guard on new buys — plus churn dragging tax and impact costs. India note: the
index-fund route (e.g. a Nifty200 Momentum 30 fund) converts monthly churn
into a single tax event — this strategy is the DIY sleeve. Universe note:
NIFTY50_UNIVERSE (20 liquid large caps) is used as a Nifty 200 proxy.
"""
from __future__ import annotations

import math

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.momentum import momentum_12_1


class Momentum121Strategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt08_momentum_12_1",
        name="Momentum Factor (12-1)",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NIFTY50_UNIVERSE"],   # Nifty 200 proxy — see docstring
        warmup_bars=270,
        params={
            "top_n": 5,           # names held equal-weight
            "buffer_rank": 10,    # exit a held name only below this rank (hold buffer)
            "min_bars": 260,      # bars required to compute 12-1 momentum
            "guard_ma": 200,      # crash guard: composite must sit above this MA
        },
        capital_required=300_000,
        max_positions=1000,        # portfolio sleeve: engine caps risk trades, not slots
        max_trades_per_day=20,
        intraday_squareoff=False,
        description=("Rank by 12-month return excluding the latest month, hold the "
                     "top 5 equal-weight, rebalance monthly with a rank-10 hold "
                     "buffer to cut churn. New buys only while the equal-weight "
                     "universe composite is above its 200-day MA (crash guard)."),
    )

    # ------------------------------------------------------------------ helpers
    def _crash_guard_ok(self, data: dict[str, pd.DataFrame]) -> bool:
        """True only when the equal-weight universe composite closes above its
        200-day MA. Composite: each symbol's closes normalized to their first
        valid value, averaged. Unconfirmable (short history) -> False."""
        closes = {sym: df.close.astype(float) for sym, df in sorted(data.items())
                  if len(df) > 0}
        if not closes:
            return False
        wide = pd.concat(closes, axis=1).sort_index()
        first = wide.apply(lambda col: col.loc[col.first_valid_index()]
                           if col.first_valid_index() is not None else float("nan"))
        normed = wide / first
        composite = normed.mean(axis=1, skipna=True).dropna()
        n = int(self.params["guard_ma"])
        if len(composite) < n:
            return False                       # cannot confirm the guard -> no new buys
        ma = composite.rolling(n, min_periods=n).mean()
        last, last_ma = float(composite.iloc[-1]), float(ma.iloc[-1])
        if math.isnan(last) or math.isnan(last_ma):
            return False
        return last > last_ma

    # ------------------------------------------------------------------ signals
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        p = self.params
        top_n = int(p["top_n"])
        buffer_rank = int(p["buffer_rank"])

        # deterministic descending momentum rank (symbol as tie-break)
        scores: dict[str, float] = {}
        for sym, df in data.items():
            if len(df) < int(p["min_bars"]):
                continue
            mom = momentum_12_1(df.close)
            if not math.isnan(mom):
                scores[sym] = mom
        ranked = sorted(scores, key=lambda s: (-scores[s], s))
        rank_of = {sym: i + 1 for i, sym in enumerate(ranked)}

        signals: list[Signal] = []
        held = {pos.symbol for pos in ctx.open_positions if pos.qty > 0}

        # exits: hold buffer — drop a held name only when it falls below buffer_rank
        exiting: set[str] = set()
        for sym in sorted(held):
            rank = rank_of.get(sym)
            if rank is None or rank > buffer_rank:
                df = data.get(sym)
                if df is None or df.empty:
                    continue                   # no bar to price the exit — degrade
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now,
                    reference_price=float(df.close.iloc[-1]),
                    product_type=ProductType.CNC,
                    reason=(f"momentum rank {rank} below buffer {buffer_rank}"
                            if rank is not None else "momentum unrankable")))
                exiting.add(sym)

        # crash guard: momentum falls hardest when regimes flip — no new buys
        # unless the equal-weight composite is above its 200-day MA
        if not ranked or not self._crash_guard_ok(data):
            return signals

        # buys: fill open slots with the top-ranked unheld names, equal weight
        kept = held - exiting
        slots = top_n - len(kept)
        for sym in ranked[:top_n]:
            if slots <= 0:
                break
            if sym in kept or sym in exiting:
                continue
            close = float(data[sym].close.iloc[-1])
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                size_hint=SizeHint(weight=1.0 / top_n),
                product_type=ProductType.CNC,
                reason=f"12-1 momentum rank {rank_of[sym]} ({scores[sym]:+.1%})"))
            slots -= 1
        return signals
