"""4.1 52-Week-High / Darvas Box Breakout.

Strength begets strength: buy new highs out of tight consolidations. A stock
within 5% of its 52-week high that has coiled into a tight 20-bar box, then
closes above the box high on expanded volume, tends to continue — supply above
is exhausted.

Regime: works in confirmed uptrends with sector confirmation; edge decays fast
in choppy or distributive tapes. Primary risk: false breakouts in weak tapes,
and wide boxes force small position size (stop distance drives sizing).

India note: avoid news-spike breakouts in low-float small-caps — circuit
filters can trap exits with no counterparty. This strategy trades the
NIFTY50_UNIVERSE large-caps only, where circuits are rare and depth is deep.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema


class Darvas52WeekHighStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw01_darvas_52wk",
        name="52-Week-High / Darvas Box Breakout",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=270,
        params={
            "near_high_pct": 5.0,       # close within this % of the 52-week high
            "box_bars": 20,             # consolidation lookback (excluding today)
            "box_max_width_pct": 8.0,   # (box_high - box_low) / close ceiling, %
            "vol_mult": 1.5,            # breakout volume vs 20-bar average (if volume exists)
            "vol_avg_bars": 20,
            "ema_period": 20,           # trend line for the stop / trend-failure exit
            "exit_ema_buffer_pct": 2.0, # exit when close is this % below the EMA
            "max_new_entries": 2,       # new entries per scan
        },
        capital_required=150_000,
        max_positions=3,
        intraday_squareoff=False,
        description=("Buy daily closes breaking above a tight 20-bar Darvas box formed "
                     "within 5% of the 52-week high, on 1.5x volume. Stop at the higher "
                     "of box low and 20-EMA; exit on close >2% below the 20-EMA. Needs "
                     "a confirmed uptrend; false breakouts are the primary risk."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        open_syms = {p.symbol for p in ctx.open_positions}
        p = self.params
        box_bars = int(p["box_bars"])
        new_entries = 0

        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue

            close = float(df.close.iloc[-1])
            ema_line = ema(df.close, int(p["ema_period"]))
            ema_now = float(ema_line.iloc[-1]) if pd.notna(ema_line.iloc[-1]) else close

            if sym in open_syms:
                # trend failure: close more than the buffer below the 20-EMA.
                # Ordinary trailing is the central ratchet's job.
                if close < ema_now * (1.0 - p["exit_ema_buffer_pct"] / 100.0):
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason=f"close >{p['exit_ema_buffer_pct']:.0f}% below {int(p['ema_period'])}-EMA"))
                continue

            if new_entries >= p["max_new_entries"]:
                continue

            # setup 1: within near_high_pct of the 52-week (252-bar) high
            hi_52wk = float(df.high.iloc[-252:].max())
            if close < hi_52wk * (1.0 - p["near_high_pct"] / 100.0):
                continue

            # setup 2: tight Darvas box over the last box_bars bars, excluding today
            box = df.iloc[-(box_bars + 1):-1]
            box_high = float(box.high.max())
            box_low = float(box.low.min())
            if (box_high - box_low) / close > p["box_max_width_pct"] / 100.0:
                continue

            # trigger: today's close breaks above the box high
            if close <= box_high:
                continue

            # volume expansion confirmation when volume data exists
            vol_ok = True
            vol_today = float(df.volume.iloc[-1])
            avg_vol = float(df.volume.iloc[-int(p["vol_avg_bars"]):].mean())
            if vol_today > 0 and avg_vol > 0:
                vol_ok = vol_today >= p["vol_mult"] * avg_vol
            if not vol_ok:
                continue

            # initial stop: box low or the 20-EMA, whichever is higher (tighter)
            stop = max(box_low, ema_now)
            if stop >= close:
                continue

            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop, product_type=ProductType.CNC,
                reason=(f"box breakout {close:.2f} > {box_high:.2f} near 52wk high "
                        f"{hi_52wk:.2f}")))
            new_entries += 1
        return signals
