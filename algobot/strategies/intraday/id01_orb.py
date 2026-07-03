"""5.1 Opening Range Breakout (ORB) — reference intraday strategy.

The first 30 minutes set the battlefield; trade the escape from it. Long on a
5-min close above the 09:15-09:45 range high (short below the low); stop at the
range midpoint; skip if the range exceeds ~0.7% of the index (stop too wide).
Executed via ATM debit verticals (compendium: the small-account defined-risk
vehicle), so the premium at risk is hard-capped.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EVERY_5MIN, StrategyBase, StrategyContext, StrategyMeta
from algobot.options.structures import vertical_spread


class ORBStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="id01_orb",
        name="Opening Range Breakout",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=["NIFTY_INDEX"],
        warmup_bars=10,
        params={
            "range_start": "09:15",
            "range_end": "09:45",
            "max_range_pct": 0.7,     # skip if opening range wider than this
            "last_entry": "14:30",
            "rr_first_target": 1.5,
            "volume_mult": 1.2,       # volume expansion vs range average (if volume exists)
            "spread_width_steps": 4,  # debit vertical width in strike steps
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=2,
        intraday_squareoff=True,
        is_multi_leg=True,
        description=("Trade the escape from the 09:15-09:45 range on a confirmed "
                     "5-min close with the range midpoint as stop. Works on trend "
                     "days seeded by overnight cues; two failed ORBs = range day."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        today = ctx.now.date()
        day = df[df.index.date == today]
        if day.empty:
            return []

        r_start = dt.time.fromisoformat(self.params["range_start"])
        r_end = dt.time.fromisoformat(self.params["range_end"])
        last_entry = dt.time.fromisoformat(self.params["last_entry"])

        bar_time = day.index[-1].time()
        if not (r_end <= bar_time <= last_entry):
            return []

        opening = day[(day.index.time >= r_start) & (day.index.time < r_end)]
        if len(opening) < 3:
            return []
        or_high, or_low = float(opening.high.max()), float(opening.low.min())
        or_mid = (or_high + or_low) / 2
        close = float(day.close.iloc[-1])

        # stop too wide -> stand aside (compendium rule)
        if (or_high - or_low) / close * 100 > self.params["max_range_pct"]:
            return []

        # volume expansion confirmation when volume data exists (index candles often lack it)
        vol_ok = True
        if day.volume.iloc[-1] > 0 and opening.volume.mean() > 0:
            vol_ok = day.volume.iloc[-1] >= self.params["volume_mult"] * opening.volume.mean()

        # only the first close beyond the range triggers (prior bars must be inside)
        after_range = day[day.index.time >= r_end]
        prior_closes = after_range.close.iloc[:-1]

        signals: list[Signal] = []
        rr = self.params["rr_first_target"]
        width = int(self.params["spread_width_steps"])

        if close > or_high and vol_ok and (prior_closes <= or_high).all():
            risk = close - or_mid
            structure = vertical_spread(
                sym, OptionType.CE, "debit",
                buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(width),
                expiry_rule=ExpiryRule.weekly())
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=or_mid, take_profit=close + rr * risk,
                structure=structure,
                reason=f"ORB long: close {close:.1f} > range high {or_high:.1f}"))
        elif close < or_low and vol_ok and (prior_closes >= or_low).all():
            risk = or_mid - close
            structure = vertical_spread(
                sym, OptionType.PE, "debit",
                buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(-width),
                expiry_rule=ExpiryRule.weekly())
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=or_mid, take_profit=close - rr * risk,
                structure=structure,
                reason=f"ORB short: close {close:.1f} < range low {or_low:.1f}"))
        return signals
