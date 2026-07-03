"""4.5 RSI Mean Reversion (within uptrends) — buy short-term fear in long-term strength.

Edge: a short-lookback RSI (2-5 period) below ~28 marks capitulative dips that
resolve upward with a high win rate but a small average winner; the exit at
RSI > ~58 or a time stop harvests the snap-back rather than a trend.
Regime: broad bull markets with rotational dips — the strategy only trades
stocks above a rising 200-day average, so it goes quiet in bears.
Risk: oversold gets more oversold in real downtrends — the 200-day filter IS
the strategy; without it this is catching falling knives. A hard stop 1.5 ATR
below entry caps the tail.
India note: with a small average winner, costs and slippage (STT, impact on
CNC round trips) decide viability — trade the most liquid names only.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.momentum import rsi
from algobot.indicators.trend import sma
from algobot.indicators.volatility import atr


class RSIMeanReversionStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw05_rsi_meanrev",
        name="RSI Mean Reversion in Uptrends",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=220,
        params={"rsi_period": 3, "rsi_entry": 28.0, "rsi_exit": 58.0,
                "max_hold_days": 8, "sma_period": 200, "sma_rising_lookback": 10,
                "atr_period": 14, "atr_mult": 1.5, "max_new_entries": 2},
        capital_required=150_000,
        max_positions=4,
        max_trades_per_day=4,
        intraday_squareoff=False,
        description=("Buys 3-period RSI < 28 dips in stocks above a rising 200-day "
                     "SMA; exits at RSI > 58 or an 8-session time stop, hard stop "
                     "1.5 ATR below entry. High win rate, small average winner — "
                     "needs broad bulls with rotational dips; the 200-day filter "
                     "is the strategy."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        p = self.params
        open_by_sym = {pos.symbol: pos for pos in ctx.open_positions}
        new_entries = 0

        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue
            close = float(df.close.iloc[-1])
            rsi_series = rsi(df.close, n=p["rsi_period"])
            rsi_now = rsi_series.iloc[-1]
            if pd.isna(rsi_now):
                continue
            rsi_now = float(rsi_now)

            pos = open_by_sym.get(sym)
            if pos is not None:
                # -------------------------------------------------- exits
                if rsi_now > p["rsi_exit"]:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason=f"fear resolved: RSI {rsi_now:.0f} > {p['rsi_exit']:.0f}"))
                    continue
                # time stop: count sessions since entry via the daily index
                entry_date = pd.Timestamp(pos.opened_at).date()
                held_sessions = int((df.index.date >= entry_date).sum())
                if held_sessions > p["max_hold_days"]:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason=f"time stop: {held_sessions} sessions held"))
                continue

            # ------------------------------------------------------ entries
            if new_entries >= p["max_new_entries"]:
                continue
            trend = sma(df.close, p["sma_period"])
            sma_now = trend.iloc[-1]
            sma_then = trend.iloc[-1 - p["sma_rising_lookback"]]
            if pd.isna(sma_now) or pd.isna(sma_then):
                continue
            in_uptrend = close > float(sma_now) and float(sma_now) > float(sma_then)
            if not in_uptrend or rsi_now >= p["rsi_entry"]:
                continue
            atr_now = atr(df, n=p["atr_period"]).iloc[-1]
            if pd.isna(atr_now) or float(atr_now) <= 0:
                continue
            stop = close - p["atr_mult"] * float(atr_now)
            if stop <= 0:
                continue
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop, product_type=ProductType.CNC,
                reason=(f"dip in uptrend: RSI({p['rsi_period']}) {rsi_now:.0f} < "
                        f"{p['rsi_entry']:.0f}, close above rising 200-day SMA")))
            new_entries += 1
        return signals
