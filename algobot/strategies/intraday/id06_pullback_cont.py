"""5.6 Pullback Continuation — the retest entry.

Same breakout idea as the ORB family, roughly half the stop, double the R.
After a confirmed intraday breakout (a 5-min close above the prior 20-bar
high earlier today), do NOT chase: wait for the pullback to retest the broken
level (or the 20-EMA on 5-min, whichever is higher) and hold — the bar's low
touches within ``retest_band_pct`` of the level but the bar closes above it.
Enter on the reclaim candle (close > open and close > level), stop just
beyond the retest low, take profit ~2R: the tight stop is the edge. Mirrored
for shorts below broken 20-bar lows.

Regime: trend days with orderly two-legged pullbacks. Primary risk: strong
moves that never pull back — accept the miss; the strategy simply doesn't
trade them. India note: the retest keeps 1R inside a fixed rupee budget,
which is what makes ATM weekly debit verticals viable on a small account.
Skips the 11:30-13:15 lunch chop and takes no entries after 14:30.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EVERY_5MIN, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema
from algobot.options.structures import vertical_spread


class PullbackContinuationStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="id06_pullback_cont",
        name="Pullback Continuation (Retest Entry)",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=["NIFTY_INDEX"],
        warmup_bars=40,
        params={
            "breakout_lookback": 20,   # prior N-bar high/low that must be broken
            "ema_len": 20,             # 5-min EMA alternative retest level
            "retest_band_pct": 0.15,   # low/high must touch within this % of level
            "stop_buffer_pct": 0.05,   # stop just beyond the retest extreme
            "rr_target": 2.0,          # ~2R take profit — the tight stop is the edge
            "lunch_start": "11:30",
            "lunch_end": "13:15",
            "last_entry": "14:30",
            "spread_width_steps": 4,   # debit vertical width in strike steps
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=2,
        intraday_squareoff=True,
        is_multi_leg=True,
        description=("After a confirmed 5-min breakout of the prior 20-bar high/low, "
                     "wait for the pullback to retest the broken level (or 20-EMA) "
                     "and reclaim it; stop beyond the retest wick, target ~2R. "
                     "Needs trend days with orderly pullbacks; V-moves are missed."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []

        today = ctx.now.date()
        day = df[df.index.date == today]
        if len(day) < 2:
            return []

        bar_time = day.index[-1].time()
        lunch_start = dt.time.fromisoformat(self.params["lunch_start"])
        lunch_end = dt.time.fromisoformat(self.params["lunch_end"])
        last_entry = dt.time.fromisoformat(self.params["last_entry"])
        if lunch_start <= bar_time < lunch_end or bar_time > last_entry:
            return []

        look = int(self.params["breakout_lookback"])
        band = self.params["retest_band_pct"] / 100.0
        buf = self.params["stop_buffer_pct"] / 100.0
        rr = self.params["rr_target"]
        width = int(self.params["spread_width_steps"])

        # prior N-bar extremes as of each bar (strictly before that bar)
        roll_high = df.high.rolling(look, min_periods=look).max().shift(1)
        roll_low = df.low.rolling(look, min_periods=look).min().shift(1)
        ema20 = ema(df.close, int(self.params["ema_len"]))

        # today's bars strictly before the current (just-closed) bar
        mask_today = df.index.date == today
        prior = df[mask_today].iloc[:-1]
        prior_rh = roll_high[mask_today].iloc[:-1]
        prior_rl = roll_low[mask_today].iloc[:-1]

        bar = day.iloc[-1]
        o, h, l, c = (float(bar.open), float(bar.high), float(bar.low), float(bar.close))
        ema_now = ema20.iloc[-1]

        signals: list[Signal] = []

        # ---- long: earlier today a 5-min close broke above the prior 20-bar high
        up = prior.close > prior_rh
        if up.any():
            # broken level = highest 20-bar-high breached today before this bar
            level = float(prior_rh[up].max())
            if pd.notna(ema_now):
                level = max(level, float(ema_now))  # retest whichever is higher
            held = level * (1 - band) <= l <= level * (1 + band)  # touched, not lost
            if held and c > level and c > o:  # reclaim candle
                stop = l * (1 - buf)  # just beyond the retest low
                risk = c - stop
                if risk > 0:
                    structure = vertical_spread(
                        sym, OptionType.CE, "debit",
                        buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(width),
                        expiry_rule=ExpiryRule.weekly())
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                        instrument=sym, timestamp=ctx.now, reference_price=c,
                        stop_loss=stop, take_profit=c + rr * risk,
                        structure=structure,
                        reason=(f"Pullback cont long: retest of {level:.1f} held "
                                f"(low {l:.1f}), reclaim close {c:.1f}")))
                    return signals

        # ---- short mirror: earlier today a 5-min close broke below the prior 20-bar low
        dn = prior.close < prior_rl
        if dn.any():
            level = float(prior_rl[dn].min())
            if pd.notna(ema_now):
                level = min(level, float(ema_now))  # retest whichever is lower
            held = level * (1 - band) <= h <= level * (1 + band)
            if held and c < level and c < o:
                stop = h * (1 + buf)
                risk = stop - c
                if risk > 0:
                    structure = vertical_spread(
                        sym, OptionType.PE, "debit",
                        buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(-width),
                        expiry_rule=ExpiryRule.weekly())
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                        instrument=sym, timestamp=ctx.now, reference_price=c,
                        stop_loss=stop, take_profit=c - rr * risk,
                        structure=structure,
                        reason=(f"Pullback cont short: retest of {level:.1f} held "
                                f"(high {h:.1f}), reclaim close {c:.1f}")))
        return signals
