"""4.4 Supertrend / ADX Trend Following — reference swing strategy.

Volatility-adjusted trailing that adapts stop distance to the instrument. Long
when price closes above Supertrend(10, 3) with ADX above 20 confirming trend
strength; the Supertrend line is the trailing stop; stand aside when ADX < 20.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import adx, supertrend


class SupertrendADXStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw04_supertrend_adx",
        name="Supertrend + ADX Trend Following",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=60,
        params={"st_period": 10, "st_mult": 3.0, "adx_period": 14, "adx_min": 20.0,
                "max_new_entries": 2},
        capital_required=150_000,
        max_positions=3,
        intraday_squareoff=False,
        description=("Long on daily close above Supertrend(10,3) with ADX>20; the "
                     "Supertrend line is the stop and trail. ATR-based stops widen "
                     "in volatility so size is recomputed from current stop distance."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        open_syms = {p.symbol for p in ctx.open_positions}
        p = self.params
        new_entries = 0

        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue
            st = supertrend(df, period=p["st_period"], mult=p["st_mult"])
            trend_strength = adx(df, n=p["adx_period"])
            close = float(df.close.iloc[-1])
            st_line = float(st.st.iloc[-1])
            direction = int(st.direction.iloc[-1])
            prev_direction = int(st.direction.iloc[-2])
            adx_now = float(trend_strength.iloc[-1]) if pd.notna(trend_strength.iloc[-1]) else 0.0

            if sym in open_syms:
                # exit on the reverse flip; trailing along the line is central R-mgmt's job
                if direction < 0:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason="supertrend flipped down"))
                continue

            if (direction > 0 and prev_direction < 0 and adx_now >= p["adx_min"]
                    and new_entries < p["max_new_entries"]):
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=st_line, product_type=ProductType.CNC,
                    reason=f"supertrend flip up, ADX {adx_now:.0f}"))
                new_entries += 1
        return signals
