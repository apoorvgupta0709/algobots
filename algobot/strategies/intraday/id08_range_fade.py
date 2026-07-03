"""5.8 Range Fade at Support/Resistance.

On confirmed range days, sell the ceiling and buy the floor — small targets,
smaller egos. The first two hours of the session define the range; once BOTH
sides have been touched at least twice, the third-plus touch of a side on a
rejection candle is faded back toward the range midpoint, with the stop just
beyond the extreme.

Edge: on genuine range days, dealer/mean-reversion flows defend well-tested
intraday levels, so a rejection at a twice-tested edge pays a small but
repeatable target to the midpoint.

Regime: expiry-adjacent drift and pre-event coiling sessions, where the index
pins inside a well-defined band and breakout attempts fail; ranges narrower
than ~0.3% are skipped because sub-0.25% targets rarely survive friction.

Primary risk: the breakout you're fading is the day's real trade — one clean
break through a faded level erases several midpoint scalps, so the playbook is
abandoned for the day as soon as any close prints beyond the range.

India note: executed via ATM weekly debit verticals (put spread at the
ceiling, call spread at the floor) so premium at risk stays hard-capped for a
small account; intraday square-off is engine-level at 15:15.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EVERY_5MIN, StrategyBase, StrategyContext, StrategyMeta
from algobot.options.structures import vertical_spread


class RangeFadeStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="id08_range_fade",
        name="Range Fade at Support/Resistance",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=["NIFTY_INDEX"],
        warmup_bars=24,               # two hours of 5-min bars
        params={
            "range_end": "11:15",     # session range = 09:15 up to this time
            "first_entry": "11:15",
            "last_entry": "14:45",
            "touch_band_pct": 0.1,    # bar extreme within this % of the level = touch
            "min_touches": 2,         # touches required on EACH side to confirm the range
            "min_width_pct": 0.3,     # skip ranges narrower than this (% of price)
            "stop_buffer_pct": 0.1,   # stop this % beyond the range extreme
            "spread_width_steps": 4,  # debit vertical width in strike steps
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=2,
        intraday_squareoff=True,
        is_multi_leg=True,
        description=("Confirmed range day (two touches of each edge of the first "
                     "two hours' range): fade the third-plus touch on a rejection "
                     "candle back to the midpoint, stop just beyond the extreme. "
                     "Abandoned for the day once any close breaks the range."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        today = ctx.now.date()
        day = df[df.index.date == today]
        if day.empty:
            return []

        r_end = dt.time.fromisoformat(self.params["range_end"])
        first_entry = dt.time.fromisoformat(self.params["first_entry"])
        last_entry = dt.time.fromisoformat(self.params["last_entry"])

        bar_time = day.index[-1].time()
        if not (first_entry <= bar_time <= last_entry):
            return []

        window = day[day.index.time < r_end]
        if len(window) < 20:  # need (nearly) the full two hours of bars
            return []
        rng_high = float(window.high.max())
        rng_low = float(window.low.min())
        rng_mid = (rng_high + rng_low) / 2
        close = float(day.close.iloc[-1])

        # too narrow -> target won't cover friction (compendium: <~0.25% rarely pays)
        if (rng_high - rng_low) / close * 100 < self.params["min_width_pct"]:
            return []

        # abandon the playbook once a side breaks: any prior close beyond the range
        prior_closes = day.close.iloc[:-1]
        if (prior_closes > rng_high).any() or (prior_closes < rng_low).any():
            return []

        band = self.params["touch_band_pct"] / 100.0
        ceiling_touch = day.high >= rng_high * (1 - band)
        floor_touch = day.low <= rng_low * (1 + band)
        min_touches = int(self.params["min_touches"])
        # range confirmation uses touches known before this bar
        if int(ceiling_touch.iloc[:-1].sum()) < min_touches:
            return []
        if int(floor_touch.iloc[:-1].sum()) < min_touches:
            return []

        bar_open = float(day.open.iloc[-1])
        buffer = self.params["stop_buffer_pct"] / 100.0
        width = int(self.params["spread_width_steps"])
        signals: list[Signal] = []

        if bool(ceiling_touch.iloc[-1]) and close < bar_open and close < rng_high:
            # third-plus touch of the ceiling on a rejection candle -> fade short
            stop = rng_high * (1 + buffer)
            structure = vertical_spread(
                sym, OptionType.PE, "debit",
                buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(-width),
                expiry_rule=ExpiryRule.weekly())
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop, take_profit=rng_mid,
                structure=structure,
                reason=(f"Range fade short: rejection at ceiling {rng_high:.1f} "
                        f"(touch #{int(ceiling_touch.sum())}), target mid {rng_mid:.1f}")))
        elif bool(floor_touch.iloc[-1]) and close > bar_open and close > rng_low:
            # third-plus touch of the floor on a rejection candle -> fade long
            stop = rng_low * (1 - buffer)
            structure = vertical_spread(
                sym, OptionType.CE, "debit",
                buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(width),
                expiry_rule=ExpiryRule.weekly())
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop, take_profit=rng_mid,
                structure=structure,
                reason=(f"Range fade long: rejection at floor {rng_low:.1f} "
                        f"(touch #{int(floor_touch.sum())}), target mid {rng_mid:.1f}")))
        return signals
