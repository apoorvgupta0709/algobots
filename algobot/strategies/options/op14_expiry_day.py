"""6.14 Expiry-Day Playbook (Tuesday Nifty).

The weekly's last hours are a different instrument: pure gamma and theta, no
time for opinions. Two mutually exclusive scripts, never run at once:

* MORNING (09:20-09:40, first trade of day only): if the open is quiet (first
  5-min bar range <= 0.4% of the index), harvest theta with a defined-risk
  iron butterfly around the pin — short ATM straddle, wings 5 steps out —
  with tight underlying stops (+/-0.6%) so a break of the pin exits the whole
  structure early.
* AFTERNOON (13:15-14:45, flat only): if a trend ignites — a 5-min close
  breaking the day's prior range with the last 3 bars all closing in the
  break direction — switch scripts: buy a cheap near-ATM option in the trend
  direction with fixed premium risk. Stop on the underlying at the broken
  range edge, target ~2R; expiry-day gamma does the rest.

Risk: whipsaw across the pin shreds late sellers; premium buyers bleed to
zero on quiet closes; STT on exercised ITM legs punishes sloppy carry.

India note: Tuesday Nifty concentrates the country's expiry flow; brokers
levy the ~2% expiry-day ELM on short option margin; square ITM shorts before
close rather than taking settlement (intraday_squareoff enforces this — no
expiry leg is ever carried into settlement).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EXPIRY_DAY, StrategyBase, StrategyContext, StrategyMeta
from algobot.options.structures import iron_butterfly, long_option


class ExpiryDayPlaybookStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op14_expiry_day",
        name="Expiry-Day Playbook (Tuesday Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EXPIRY_DAY,
        instruments=["NIFTY_INDEX"],
        warmup_bars=10,
        params={
            "morning_start": "09:20",
            "morning_end": "09:40",
            "quiet_open_pct": 0.4,     # first-bar range cap for the butterfly script
            "fly_wing_steps": 5,
            "fly_stop_pct": 0.6,       # underlying stop/target band around entry
            "afternoon_start": "13:15",
            "afternoon_end": "14:45",
            "confirm_bars": 3,         # bars that must close in the break direction
            "rr_target": 2.0,          # take-profit at ~2R on the underlying
        },
        capital_required=150_000,
        max_positions=1,
        max_trades_per_day=2,
        intraday_squareoff=True,       # never carry expiry legs into settlement
        is_multi_leg=True,
        description=("Expiry-day two-script playbook: morning theta harvest via "
                     "an iron butterfly around the pin on quiet opens; afternoon "
                     "long cheap near-ATM options if a trend ignites after 13:15. "
                     "Never both at once; whipsaw across the pin is the risk."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        p = self.params

        today = ctx.now.date()
        day = df[df.index.date == today]
        if day.empty:
            return []

        bar_time = day.index[-1].time()
        close = float(day.close.iloc[-1])

        am_start = dt.time.fromisoformat(p["morning_start"])
        am_end = dt.time.fromisoformat(p["morning_end"])
        pm_start = dt.time.fromisoformat(p["afternoon_start"])
        pm_end = dt.time.fromisoformat(p["afternoon_end"])

        # -------- MORNING SCRIPT: theta harvest around the pin --------------
        if am_start <= bar_time <= am_end:
            if ctx.trades_today != 0:
                return []
            first = day.iloc[0]
            quiet = (float(first.high) - float(first.low)) / close * 100 <= p["quiet_open_pct"]
            if not quiet:
                return []
            band = close * p["fly_stop_pct"] / 100.0
            structure = iron_butterfly(sym, wing_steps=int(p["fly_wing_steps"]),
                                       expiry_rule=ExpiryRule.weekly(0))
            return [Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=close + band, take_profit=close - band,
                structure=structure,
                tags={"script": "morning_butterfly"},
                reason=(f"expiry-day quiet open: first-bar range "
                        f"{(float(first.high) - float(first.low)) / close * 100:.2f}% "
                        f"<= {p['quiet_open_pct']}%, butterfly around {close:.1f}"))]

        # -------- AFTERNOON SCRIPT: trend-ignition premium buy --------------
        if not (pm_start <= bar_time <= pm_end):
            return []
        if ctx.trades_today >= self.meta.max_trades_per_day:
            return []

        n_confirm = int(p["confirm_bars"])
        earlier = day.iloc[:-1]
        if len(earlier) < n_confirm + 1:
            return []
        prior_high = float(earlier.high.max())
        prior_low = float(earlier.low.min())
        last = day.tail(n_confirm)

        rr = p["rr_target"]
        if close > prior_high and bool((last.close > last.open).all()):
            risk = close - prior_high
            if risk <= 0:
                return []
            structure = long_option(sym, OptionType.CE, StrikeRule.atm(0),
                                    expiry_rule=ExpiryRule.weekly(0))
            return [Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=prior_high, take_profit=close + rr * risk,
                structure=structure,
                tags={"script": "afternoon_ignition", "direction": "up"},
                reason=(f"expiry-day ignition long: close {close:.1f} > day high "
                        f"{prior_high:.1f}, {n_confirm} up closes"))]
        if close < prior_low and bool((last.close < last.open).all()):
            risk = prior_low - close
            if risk <= 0:
                return []
            structure = long_option(sym, OptionType.PE, StrikeRule.atm(0),
                                    expiry_rule=ExpiryRule.weekly(0))
            return [Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=prior_low, take_profit=close - rr * risk,
                structure=structure,
                tags={"script": "afternoon_ignition", "direction": "down"},
                reason=(f"expiry-day ignition short: close {close:.1f} < day low "
                        f"{prior_low:.1f}, {n_confirm} down closes"))]
        return []
