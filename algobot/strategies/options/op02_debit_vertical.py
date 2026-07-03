"""6.2 Debit Vertical Spreads (bull call / bear put).

The small-account workhorse: buy direction, sell away the part you don't
need, cap everything. Buy the ATM option, sell one a few strikes further
out, same weekly expiry — net debit is the maximum loss, known before
entry. The defined-risk rupee cap is exactly how small accounts enforce
per-trade risk: a Rs 20-25 wide Nifty spread costs ~Rs 550-900/lot.

Edge: directional exposure at a hard-capped, pre-known cost.
Regime: directional 2-10 day swings; especially useful in elevated IV,
where the short leg funds the long and offsets premium decay.
Risk: capped reward means the win-rate must carry more of the P&L; wide
bid-asks on the far legs eat edge — liquid index strikes only. Stop is on
the underlying at the 20-day EMA (the thesis line); target ~2R.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema
from algobot.options.structures import vertical_spread


class DebitVerticalStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op02_debit_vertical",
        name="Debit Vertical Spread (Nifty swing)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=40,
        params={
            "ema_len": 20,             # thesis line: trend filter + underlying stop
            "slope_lookback": 5,       # EMA rising/falling vs this many bars ago
            "breakout_lookback": 10,   # prior N-day high/low breakout trigger
            "spread_width_steps": 3,   # sell leg this many strike steps beyond ATM
            "min_days_to_expiry": 2,   # below this, roll entry to next weekly
            "rr_target": 2.0,          # take-profit at ~2R on the underlying
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Buy ATM / sell 3 strikes out on the Nifty weekly when the "
                     "tape turns: close beyond a rising/falling 20-EMA plus a "
                     "10-day high/low break. Net debit caps the loss; stop on the "
                     "underlying at the 20-EMA, target ~2R."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        p = self.params
        if len(df) < self.meta.warmup_bars:
            return []

        close = float(df.close.iloc[-1])
        e = ema(df.close, int(p["ema_len"]))
        ema_now = float(e.iloc[-1])
        ema_prev = float(e.iloc[-1 - int(p["slope_lookback"])])
        if pd.isna(ema_now) or pd.isna(ema_prev):
            return []

        # prior N-day extremes (exclude the signal bar itself)
        lb = int(p["breakout_lookback"])
        prior_high = float(df.high.iloc[-1 - lb:-1].max())
        prior_low = float(df.low.iloc[-1 - lb:-1].min())

        # expiry-cycle guard: too close to the weekly expiry -> use the next one
        try:
            from algobot.data.expiries import days_to_expiry
            dte = days_to_expiry("NIFTY", "weekly", on_date=ctx.now.date())
        except Exception:
            dte = 4  # data layer unavailable (isolated backtest) -> assume mid-cycle
        expiry_rule = ExpiryRule.weekly(1) if dte < int(p["min_days_to_expiry"]) \
            else ExpiryRule.weekly()

        width = int(p["spread_width_steps"])
        rr = float(p["rr_target"])
        signals: list[Signal] = []

        if close > ema_now and ema_now > ema_prev and close > prior_high:
            # bull call spread: tape turned up
            risk = close - ema_now
            structure = vertical_spread(
                sym, OptionType.CE, "debit",
                buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(width),
                expiry_rule=expiry_rule)
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=ema_now, take_profit=close + rr * risk,
                structure=structure,
                reason=(f"bull call spread: close {close:.1f} > rising EMA20 "
                        f"{ema_now:.1f}, broke {lb}-day high {prior_high:.1f}, "
                        f"dte {dte}")))
        elif close < ema_now and ema_now < ema_prev and close < prior_low:
            # bear put spread: mirror on the downside
            risk = ema_now - close
            structure = vertical_spread(
                sym, OptionType.PE, "debit",
                buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(-width),
                expiry_rule=expiry_rule)
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=ema_now, take_profit=close - rr * risk,
                structure=structure,
                reason=(f"bear put spread: close {close:.1f} < falling EMA20 "
                        f"{ema_now:.1f}, broke {lb}-day low {prior_low:.1f}, "
                        f"dte {dte}")))
        return signals
