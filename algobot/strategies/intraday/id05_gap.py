"""5.5 Gap Trading (gap-and-go / gap-fill).

Opening gaps either trend (institutional repricing) or fill (emotional
overshoot) — trade the tell, not the guess. Gap = today's first 5-min bar open
vs the prior session's last close. The first 15 minutes decide the script:

- GAP-AND-GO: |gap| >= ~0.4% and after the first 15 minutes price still holds
  beyond the first-15-min low (high for gap-downs) -> go WITH the gap on a
  5-min close breaking out of the first-30-min consolidation, stop at the
  first-15-min extreme.
- GAP-FILL: a modest |gap| (~0.15-0.35%) whose first two bars immediately lose
  the open (close against the gap direction) -> fade toward yesterday's close
  (take_profit = prior close), stop beyond the day's extreme so far.

Edge: the market tells you within 15 minutes whether the gap was information
or emotion; both scripts are one-shot, defined-risk continuation/fade trades.
Regime: needs a real overnight gap; dead-flat opens produce nothing. Entries
only 09:30-11:30, one trade per day.
Primary risk: fading a genuine repricing gap — a "small" gap on real news
never fills and the fade stops out at the day extreme.
India note: GIFT Nifty telegraphs most gaps before 09:15; the trade is not the
gap itself but how the cash open treats it. Executed via ATM debit verticals
(defined-risk small-account vehicle), like the ORB reference.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EVERY_5MIN, StrategyBase, StrategyContext, StrategyMeta
from algobot.options.structures import vertical_spread


class GapStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="id05_gap",
        name="Gap Trading (Gap-and-Go / Gap-Fill)",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=["NIFTY_INDEX"],
        warmup_bars=80,               # need the full prior session for its close
        params={
            "go_min_pct": 0.4,        # |gap| >= this => gap-and-go script
            "fill_min_pct": 0.15,     # fill script gap band, lower bound
            "fill_max_pct": 0.35,     # fill script gap band, upper bound
            "decide_end": "09:30",    # first 15 minutes decide the script
            "consol_end": "09:45",    # first-30-min consolidation for go entries
            "entry_start": "09:30",   # entries only 09:30-11:30 (bar labels)
            "entry_end": "11:30",
            "rr_go_target": 2.0,      # gap-and-go take-profit in R multiples
            "stop_buffer_pct": 0.05,  # fill stop placed beyond day extreme by this
            "spread_width_steps": 4,  # debit vertical width in strike steps
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=True,
        is_multi_leg=True,
        description=("Trade the tell of the opening gap: >=0.4% gaps that hold the "
                     "first-15-min extreme go WITH the gap on a first-30-min-range "
                     "break; 0.15-0.35% gaps that immediately lose the open are faded "
                     "to yesterday's close. Risk: fading a genuine repricing gap."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position or ctx.trades_today >= self.meta.max_trades_per_day:
            return []

        sym, df = next(iter(data.items()))
        today = ctx.now.date()
        day = df[df.index.date == today]
        prev = df[df.index.date < today]
        if day.empty or prev.empty:
            return []

        entry_start = dt.time.fromisoformat(self.params["entry_start"])
        entry_end = dt.time.fromisoformat(self.params["entry_end"])
        decide_end = dt.time.fromisoformat(self.params["decide_end"])
        consol_end = dt.time.fromisoformat(self.params["consol_end"])

        bar_time = day.index[-1].time()
        if not (entry_start <= bar_time <= entry_end):
            return []

        first15 = day[day.index.time < decide_end]
        if len(first15) < 3:                      # first 15 minutes must be complete
            return []

        prev_close = float(prev.close.iloc[-1])
        day_open = float(day.open.iloc[0])
        gap_pct = (day_open - prev_close) / prev_close * 100.0
        close = float(day.close.iloc[-1])
        f15_high = float(first15.high.max())
        f15_low = float(first15.low.min())
        width = int(self.params["spread_width_steps"])

        def _vertical(opt_type: OptionType, sell_steps: int):
            return vertical_spread(
                sym, opt_type, "debit",
                buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(sell_steps),
                expiry_rule=ExpiryRule.weekly())

        # ------------------------------------------------------- gap-and-go
        if abs(gap_pct) >= self.params["go_min_pct"]:
            consol = day[day.index.time < consol_end]
            after = day[day.index.time >= consol_end]
            if after.empty:                       # consolidation not finished yet
                return []
            post15 = day[day.index.time >= decide_end]
            prior_closes = after.close.iloc[:-1]  # one-shot: first break only
            rr = self.params["rr_go_target"]

            if gap_pct > 0:
                held = float(post15.low.min()) > f15_low
                c_high = float(consol.high.max())
                if held and close > c_high and (prior_closes <= c_high).all():
                    risk = close - f15_low
                    return [Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        stop_loss=f15_low, take_profit=close + rr * risk,
                        structure=_vertical(OptionType.CE, width),
                        reason=(f"Gap-and-go long: gap {gap_pct:+.2f}%, close {close:.1f} "
                                f"> 30-min high {c_high:.1f}, held 15-min low {f15_low:.1f}"))]
            else:
                held = float(post15.high.max()) < f15_high
                c_low = float(consol.low.min())
                if held and close < c_low and (prior_closes >= c_low).all():
                    risk = f15_high - close
                    return [Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        stop_loss=f15_high, take_profit=close - rr * risk,
                        structure=_vertical(OptionType.PE, -width),
                        reason=(f"Gap-and-go short: gap {gap_pct:+.2f}%, close {close:.1f} "
                                f"< 30-min low {c_low:.1f}, held 15-min high {f15_high:.1f}"))]
            return []

        # --------------------------------------------------------- gap-fill
        if not (self.params["fill_min_pct"] <= abs(gap_pct) <= self.params["fill_max_pct"]):
            return []

        buf = 1.0 + self.params["stop_buffer_pct"] / 100.0
        c0, c1 = float(day.close.iloc[0]), float(day.close.iloc[1])

        if gap_pct > 0 and c0 < day_open and c1 < day_open:
            # gapped up, immediately lost the open -> fade down to prior close
            if prev_close < close < day_open:     # gap still open, still below open
                stop = float(day.high.max()) * buf
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=prev_close,
                    structure=_vertical(OptionType.PE, -width),
                    reason=(f"Gap-fill short: gap {gap_pct:+.2f}% lost the open "
                            f"{day_open:.1f}; fading to prior close {prev_close:.1f}"))]
        elif gap_pct < 0 and c0 > day_open and c1 > day_open:
            # gapped down, immediately reclaimed the open -> fade up to prior close
            if day_open < close < prev_close:     # gap still open, still above open
                stop = float(day.low.min()) / buf
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=prev_close,
                    structure=_vertical(OptionType.CE, width),
                    reason=(f"Gap-fill long: gap {gap_pct:+.2f}% reclaimed the open "
                            f"{day_open:.1f}; fading to prior close {prev_close:.1f}"))]
        return []
