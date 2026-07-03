"""4.3 Moving-Average Crossover System.

A fully mechanical trend harvester: dumb, robust, and honest. Long when the
fast EMA (20) crosses above the slow EMA (50); flat when it crosses back
below. A positional 50/200 golden-cross variant is selectable via params.

Edge: never top-ticks and never bottom-ticks, but never misses the big move
either — it captures the fat middle of long clean trends. Expect 40-50% win
rates saved by 2.5R+ winners; the central ratchet trail and the reverse cross
do the exits, so there is no take_profit and no discretion.

Regime: needs long clean trends. Primary risk: death by whipsaw in ranges,
where every cross reverses two bars later and costs a small loss each time.

India note: the low trade frequency is precisely why this survives costs
(STT, impact) on Nifty and liquid F&O large caps — a handful of round trips a
year per name keeps the cost drag negligible relative to trend capture.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import crossover, ema


class MACrossoverStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw03_ma_crossover",
        name="Moving-Average Crossover System",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=120,
        params={"fast": 20, "slow": 50, "stop_buffer_pct": 0.01,
                "max_new_entries": 2},
        capital_required=150_000,
        max_positions=4,
        intraday_squareoff=False,
        description=("Long on the fast EMA crossing above the slow EMA (20/50 "
                     "default, 50/200 positional variant via params); stop "
                     "trails under the slower average, exit on the reverse "
                     "cross. No take-profit — winners run until the trend dies."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        open_syms = {p.symbol for p in ctx.open_positions}
        p = self.params
        fast_n, slow_n = int(p["fast"]), int(p["slow"])
        new_entries = 0

        for sym, df in data.items():
            # guard covers the 50/200 positional variant, where warmup_bars=120
            # alone is not enough history for a stable slow EMA
            if len(df) < self.meta.warmup_bars or len(df) < slow_n * 1.2:
                continue
            fast_ema = ema(df.close, fast_n)
            slow_ema = ema(df.close, slow_n)
            cross = int(crossover(fast_ema, slow_ema).iloc[-1])
            close = float(df.close.iloc[-1])
            slow_now = slow_ema.iloc[-1]
            if pd.isna(slow_now):
                continue

            if sym in open_syms:
                # mechanical exit: the reverse cross, no discretion, no exceptions
                if cross == -1:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason=f"EMA{fast_n} crossed below EMA{slow_n}"))
                continue

            if cross == 1 and new_entries < p["max_new_entries"]:
                stop = float(slow_now) * (1.0 - p["stop_buffer_pct"])
                if stop >= close:
                    continue  # degenerate cross: stop must sit below the entry
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, product_type=ProductType.CNC,
                    reason=f"EMA{fast_n} crossed above EMA{slow_n}"))
                new_entries += 1
        return signals
