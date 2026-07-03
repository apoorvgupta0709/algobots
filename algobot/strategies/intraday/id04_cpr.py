"""5.4 CPR (Central Pivot Range) Framework.

Yesterday's pivot maths classifies today before it opens: trend day or range
day. Pivot/BC/TC come from the prior session's H/L/C (pivot=(H+L+C)/3,
BC=(H+L)/2, TC=2*pivot-BC). A narrow CPR biases a trend day — trade breakouts
beyond the prior-day extremes (or beyond TC/BC with 3-bar momentum), stop at
the pivot. A wide CPR biases a range day — fade rejection candles that poke
beyond TC/BC back toward the pivot. In-between widths: stand aside.

Edge: CPR is watched by a large crowd on Nifty/BankNifty, so the levels are
partly self-fulfilling — breakouts from narrow ranges attract follow-through
and pokes beyond a wide range attract fading.
Regime: needs the prior session to be informative — works best in stable
overnight conditions; gap-and-go days can invalidate the classification.
Risk: the width bias is a probability, not a certainty — a narrow CPR can
still produce a chop day and a wide CPR can trend right through TC; the pivot
stop (trend) and beyond-day-extreme stop (range) cap each mistake. Executed
via ATM debit verticals so premium at risk is hard-capped. India note: lunch
chop (11:30-13:15) is skipped and no entries after 14:30.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EVERY_5MIN, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.levels import cpr
from algobot.options.structures import vertical_spread


class CPRFrameworkStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="id04_cpr",
        name="CPR Trend/Range Framework",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=["NIFTY_INDEX"],
        warmup_bars=80,               # need the full prior session plus today
        params={
            "narrow_pct": 0.18,       # CPR width < this (% of pivot) -> trend-day bias
            "wide_pct": 0.35,         # CPR width > this -> range-day bias
            "momentum_bars": 3,       # trend entry via TC/BC needs close beyond N-bar extreme
            "lunch_start": "11:30",
            "lunch_end": "13:15",
            "last_entry": "14:30",
            "stop_buffer_pct": 0.05,  # range-day stop this % beyond the day extreme
            "rr_trend_target": 2.0,   # trend-day target in R multiples
            "spread_width_steps": 4,  # debit vertical width in strike steps
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=2,
        intraday_squareoff=True,
        is_multi_leg=True,
        description=("Classify the day from the prior session's CPR width before "
                     "trading it: narrow CPR -> trend day, trade breakouts beyond the "
                     "prior-day extremes with the pivot as stop; wide CPR -> range day, "
                     "fade TC/BC rejection candles back to the pivot. Bias is a "
                     "probability, not a certainty."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        if df.empty:
            return []

        dates = np.asarray(df.index.date)
        today = ctx.now.date()
        day = df[dates == today]
        if len(day) < 3:
            return []

        # prior session from the same 5-min frame (last full session before today)
        prev_dates = np.unique(dates[dates < today])
        if len(prev_dates) == 0:
            return []
        prev = df[dates == prev_dates[-1]]
        if len(prev) < 10:
            return []
        prev_high = float(prev.high.max())
        prev_low = float(prev.low.min())
        prev_close = float(prev.close.iloc[-1])
        levels = cpr(prev_high, prev_low, prev_close)
        pivot, bc, tc, width_pct = (
            levels["pivot"], levels["bc"], levels["tc"], levels["width_pct"])
        if not np.isfinite(width_pct):
            return []

        # session-time filters: lunch chop skipped, hard last-entry cutoff
        bar_time = day.index[-1].time()
        lunch_start = dt.time.fromisoformat(self.params["lunch_start"])
        lunch_end = dt.time.fromisoformat(self.params["lunch_end"])
        last_entry = dt.time.fromisoformat(self.params["last_entry"])
        if bar_time > last_entry or (lunch_start <= bar_time <= lunch_end):
            return []

        bar = day.iloc[-1]
        close = float(bar.close)
        bar_open = float(bar.open)
        bar_high = float(bar.high)
        bar_low = float(bar.low)
        day_high = float(day.high.max())
        day_low = float(day.low.min())
        mom_n = int(self.params["momentum_bars"])
        width = int(self.params["spread_width_steps"])
        rr = float(self.params["rr_trend_target"])
        buf = float(self.params["stop_buffer_pct"]) / 100.0 * close

        signals: list[Signal] = []

        if width_pct < self.params["narrow_pct"]:
            # ---- TREND-DAY bias: trade the breakout, stop at the pivot ----
            if len(day) < mom_n + 1:
                return []
            recent_high = float(day.high.iloc[-(mom_n + 1):-1].max())
            recent_low = float(day.low.iloc[-(mom_n + 1):-1].min())
            prev_bar_close = float(day.close.iloc[-2])

            long_break = close > prev_high
            long_momo = close > tc and close > recent_high
            short_break = close < prev_low
            short_momo = close < bc and close < recent_low
            # one-shot: previous bar must not already have been through the trigger
            long_fresh = prev_bar_close <= max(prev_high, tc)
            short_fresh = prev_bar_close >= min(prev_low, bc)

            if (long_break or long_momo) and long_fresh and pivot < close:
                risk = close - pivot
                structure = vertical_spread(
                    sym, OptionType.CE, "debit",
                    buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(width),
                    expiry_rule=ExpiryRule.weekly())
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=pivot, take_profit=close + rr * risk,
                    structure=structure,
                    reason=(f"CPR trend long: width {width_pct:.2f}% narrow, close "
                            f"{close:.1f} > {'PDH ' + format(prev_high, '.1f') if long_break else 'TC ' + format(tc, '.1f')}")))
            elif (short_break or short_momo) and short_fresh and pivot > close:
                risk = pivot - close
                structure = vertical_spread(
                    sym, OptionType.PE, "debit",
                    buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(-width),
                    expiry_rule=ExpiryRule.weekly())
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=pivot, take_profit=close - rr * risk,
                    structure=structure,
                    reason=(f"CPR trend short: width {width_pct:.2f}% narrow, close "
                            f"{close:.1f} < {'PDL ' + format(prev_low, '.1f') if short_break else 'BC ' + format(bc, '.1f')}")))

        elif width_pct > self.params["wide_pct"]:
            # ---- RANGE-DAY bias: fade rejection pokes beyond TC/BC to the pivot ----
            reject_up = bar_high > tc and close < bar_open and close > pivot
            reject_dn = bar_low < bc and close > bar_open and close < pivot

            if reject_up:
                stop = day_high + buf
                if stop > close:
                    structure = vertical_spread(
                        sym, OptionType.PE, "debit",
                        buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(-width),
                        expiry_rule=ExpiryRule.weekly())
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        stop_loss=stop, take_profit=pivot,
                        structure=structure,
                        reason=(f"CPR range fade short: width {width_pct:.2f}% wide, "
                                f"rejection above TC {tc:.1f}, target pivot {pivot:.1f}")))
            elif reject_dn:
                stop = day_low - buf
                if stop < close:
                    structure = vertical_spread(
                        sym, OptionType.CE, "debit",
                        buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(width),
                        expiry_rule=ExpiryRule.weekly())
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        stop_loss=stop, take_profit=pivot,
                        structure=structure,
                        reason=(f"CPR range fade long: width {width_pct:.2f}% wide, "
                                f"rejection below BC {bc:.1f}, target pivot {pivot:.1f}")))

        # between narrow and wide: no classification -> stand aside
        return signals
