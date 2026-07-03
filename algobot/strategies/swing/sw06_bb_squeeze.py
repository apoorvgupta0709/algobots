"""4.6 Bollinger Bands — Squeeze and Reversion.

One indicator, two regimes: fade the band in ranges, chase the break after
squeezes. The two modes are mutually exclusive; classify the regime first.

Edge: band width at a 120-day minimum marks compressed volatility that tends
to resolve in an expansion move — chase the close above the upper band with
the opposite (lower) band as stop and no target, letting the central ratchet
trail the expansion. When there is no squeeze and the tape is flat (close
within 3% of the 50-day SMA), band pokes are noise: fade a close below the
lower band on a reversal candle (close > open) back to the 20-day mid band,
with a 1.5x ATR(14) stop.

Regime: reversion works in sideways large-caps; squeezes cluster ahead of
results/events — index-future squeeze breaks around policy events are a
recurring pattern.

Primary risk: using the reversion playbook on a squeeze day — a lower-band
close during a squeeze is the start of an expansion, not a fade. The modes
are mutually exclusive, so squeeze classification always runs first and
suppresses reversion entries.

India note: LONG-only — cash-market shorts are intraday-only in India;
positional shorts need futures/puts, omitted here. CNC delivery product.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import sma
from algobot.indicators.volatility import atr, bb_squeeze, bollinger


class BollingerSqueezeReversionStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw06_bb_squeeze",
        name="Bollinger Squeeze Break + Range Reversion",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=150,
        params={"bb_n": 20, "bb_k": 2.0, "squeeze_lookback": 120,
                "squeeze_recent_bars": 5, "sma_n": 50, "flat_band_pct": 0.03,
                "atr_n": 14, "atr_mult": 1.5, "max_new_entries": 2},
        capital_required=150_000,
        max_positions=3,
        intraday_squareoff=False,
        description=("Two mutually exclusive Bollinger regimes: after a 120-day "
                     "width squeeze, buy the close above the upper band (stop = "
                     "lower band, no target — ratchet trails); otherwise, in a "
                     "flat tape near the 50-SMA, fade a lower-band close on a "
                     "reversal candle back to the mid band. Squeeze wins."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        p = self.params
        open_by_sym = {pos.symbol: pos for pos in ctx.open_positions}
        new_entries = 0

        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue

            close_s = df["close"].astype(float)
            bands = bollinger(close_s, n=p["bb_n"], k=p["bb_k"])
            close = float(close_s.iloc[-1])
            open_px = float(df["open"].iloc[-1])
            mid = bands["mid"].iloc[-1]
            upper = bands["upper"].iloc[-1]
            lower = bands["lower"].iloc[-1]
            if pd.isna(mid) or pd.isna(upper) or pd.isna(lower):
                continue
            mid, upper, lower = float(mid), float(upper), float(lower)

            # ---- exits for held names --------------------------------------
            pos = open_by_sym.get(sym)
            if pos is not None:
                # squeeze-mode positions carry no take_profit; expansion is
                # over once price loses the mid band. Reversion positions
                # leave exits to the engine's TP/SL.
                if pos.take_profit is None and close < mid:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason="close below mid band — expansion over"))
                continue

            if new_entries >= p["max_new_entries"]:
                continue

            # ---- classify the regime FIRST: squeeze wins, never both ------
            sq = bb_squeeze(close_s, n=p["bb_n"], k=p["bb_k"],
                            lookback=p["squeeze_lookback"])
            squeezed = bool(sq.iloc[-p["squeeze_recent_bars"]:].fillna(False).any())

            if squeezed:
                # SQUEEZE MODE: chase the expansion break above the upper
                # band; the opposite (lower) band is the stop; no target.
                prev_close = float(close_s.iloc[-2])
                prev_upper = bands["upper"].iloc[-2]
                broke_out = (close > upper
                             and pd.notna(prev_upper)
                             and prev_close <= float(prev_upper))
                if broke_out and lower < close:
                    signals.append(Signal(
                        strategy_id=self.strategy_id,
                        signal_type=SignalType.ENTRY_LONG,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        stop_loss=lower, take_profit=None,
                        product_type=ProductType.CNC,
                        reason="squeeze expansion break above upper band"))
                    new_entries += 1
                continue  # never run reversion on a squeeze bar

            # REVERSION MODE: no squeeze AND flat tape near the 50-day SMA.
            sma50 = sma(close_s, p["sma_n"]).iloc[-1]
            if pd.isna(sma50) or abs(close / float(sma50) - 1.0) >= p["flat_band_pct"]:
                continue
            atr_now = atr(df, n=p["atr_n"]).iloc[-1]
            if pd.isna(atr_now):
                continue
            reversal_candle = close > open_px
            if close < lower and reversal_candle:
                stop = close - p["atr_mult"] * float(atr_now)
                signals.append(Signal(
                    strategy_id=self.strategy_id,
                    signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=mid,
                    product_type=ProductType.CNC,
                    reason="lower-band fade with reversal candle in flat tape"))
                new_entries += 1

        return signals
