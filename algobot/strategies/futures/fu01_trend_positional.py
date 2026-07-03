"""7.1 Positional Futures Trend Trading — reference futures strategy.

Linear leverage on the index ridden with a volatility trail: Section 4 trend
signals (Supertrend) expressed in index futures, one lot per signal, stop via
the Supertrend line, rolled before expiry, ratchet-trailed by the central
R-management system.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, ProductType, Side, SignalType, Timeframe
from algobot.core.models import ExpiryRule, OptionLeg, OptionStructure, Signal, StrikeRule
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import adx, supertrend


def _futures_structure(underlying: str, side: Side) -> OptionStructure:
    """Single-leg futures position expressed as a structure (monthly contract)."""
    return OptionStructure(
        name="index_future",
        underlying=underlying,
        legs=[OptionLeg(side=side, option_type=OptionType.FUT,
                        strike_rule=StrikeRule.absolute(0),
                        expiry_rule=ExpiryRule.monthly())],
        net_direction="debit",
    )


class FuturesTrendStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="fu01_trend_positional",
        name="Positional Futures Trend",
        category=Category.FUTURES,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=60,
        params={"st_period": 10, "st_mult": 3.0, "adx_period": 14, "adx_min": 18.0},
        capital_required=1_000_000,   # ~2.4L margin/lot + drawdown headroom under 1% risk
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Daily Supertrend(10,3) with ADX filter on the index, expressed "
                     "as one monthly futures lot per signal, MARGIN carry. Stop rides "
                     "the Supertrend line; the engine's ratchet trails winners. "
                     "Overnight gap risk on ~16L notional — capital gate is strict."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        p = self.params
        st = supertrend(df, period=p["st_period"], mult=p["st_mult"])
        trend_strength = adx(df, n=p["adx_period"])
        close = float(df.close.iloc[-1])
        st_line = float(st.st.iloc[-1])
        direction = int(st.direction.iloc[-1])
        prev_direction = int(st.direction.iloc[-2])
        adx_now = float(trend_strength.iloc[-1]) if pd.notna(trend_strength.iloc[-1]) else 0.0

        long_open = any(p_.is_long for p_ in ctx.open_positions)
        short_open = any(not p_.is_long for p_ in ctx.open_positions)

        signals: list[Signal] = []
        # exits on the reverse flip
        if long_open and direction < 0:
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                product_type=ProductType.MARGIN, reason="supertrend flipped down"))
        if short_open and direction > 0:
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                product_type=ProductType.MARGIN, reason="supertrend flipped up"))

        if ctx.has_open_position or adx_now < p["adx_min"]:
            return signals

        if direction > 0 and prev_direction < 0:
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=st_line, structure=_futures_structure(sym, Side.BUY),
                product_type=ProductType.MARGIN,
                reason=f"trend flip up, ADX {adx_now:.0f}"))
        elif direction < 0 and prev_direction > 0:
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=st_line, structure=_futures_structure(sym, Side.SELL),
                product_type=ProductType.MARGIN,
                reason=f"trend flip down, ADX {adx_now:.0f}"))
        return signals
