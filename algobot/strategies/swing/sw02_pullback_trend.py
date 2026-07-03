"""4.2 Pullback-to-Trend — buy dips in uptrends.

Edge: enter established trends at discounts to the last impulse, not at its
top; the tight stop below the pullback low doubles the R-multiple versus
chasing the breakout. Regime: steady trends — price above a rising 50-EMA
making higher highs. Primary risk: the pullback that becomes the reversal,
which is why the reversal candle trigger is mandatory (never catch a falling
knife). India note: works on index heavyweights and liquid mid-caps; illiquid
names gap through pullback stops.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema


class PullbackTrendStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw02_pullback_trend",
        name="Pullback to Trend (EMA dip-buy)",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=80,
        params={"ema_fast": 20, "ema_slow": 50, "slope_lookback": 5,
                "hh_window": 20, "clear_bars": 5, "stop_buffer": 0.995,
                "rr_target": 2.5, "max_new_entries": 2},
        capital_required=150_000,
        max_positions=3,
        intraday_squareoff=False,
        description=("Buy the first touch of the 20-EMA in a qualified uptrend "
                     "(price above a rising 50-EMA with higher highs) only when a "
                     "reversal candle prints; stop below the pullback low, ~2.5R "
                     "target, central ratchet trails; exit on close below the 50-EMA."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        open_syms = {p.symbol for p in ctx.open_positions}
        p = self.params
        new_entries = 0

        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue

            ema20 = ema(df.close, p["ema_fast"])
            ema50 = ema(df.close, p["ema_slow"])
            if pd.isna(ema50.iloc[-1]) or pd.isna(ema50.iloc[-1 - p["slope_lookback"]]):
                continue

            close = float(df.close.iloc[-1])
            open_ = float(df.open.iloc[-1])
            low = float(df.low.iloc[-1])
            ema20_now = float(ema20.iloc[-1])
            ema50_now = float(ema50.iloc[-1])

            if sym in open_syms:
                # trend qualification broken: close below the 50-EMA
                if close < ema50_now:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason="close below 50-EMA, trend broken"))
                continue

            if new_entries >= p["max_new_entries"]:
                continue

            # -- qualify the uptrend --------------------------------------
            rising_ema50 = ema50_now > float(ema50.iloc[-1 - p["slope_lookback"]])
            w = p["hh_window"]
            recent_high = float(df.high.iloc[-w:].max())
            prior_high = float(df.high.iloc[-2 * w:-w].max())
            higher_highs = recent_high > prior_high
            if not (close > ema50_now and rising_ema50 and higher_highs):
                continue

            # -- first touch of the 20-EMA --------------------------------
            n = p["clear_bars"]
            touch = low <= ema20_now
            stayed_above = bool(
                (df.low.iloc[-1 - n:-1].values > ema20.iloc[-1 - n:-1].values).all())
            # -- mandatory reversal candle: never catch a falling knife ---
            reversal = close > open_ and close > ema20_now
            if not (touch and stayed_above and reversal):
                continue

            stop = low * p["stop_buffer"]           # below the pullback low
            if stop >= close:
                continue
            target = close + p["rr_target"] * (close - stop)
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop, take_profit=target, product_type=ProductType.CNC,
                reason="first 20-EMA touch in rising trend, reversal candle"))
            new_entries += 1
        return signals
