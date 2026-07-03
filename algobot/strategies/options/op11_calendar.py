"""6.11 Calendar Spread (Nifty, same-strike horizontal).

Sell fast time, own slow time: short the near expiry, long the far, same
strike. Sell the Tuesday weekly ATM call and buy the monthly at the same
strike for a net debit; profit as near-term theta outruns far-term while the
index stays pinned to the strike.

Edge: theta decay is steepest in the final days of an option's life, so the
short weekly bleeds faster than the long monthly — the spread widens if the
underlying sits still.
Regime it needs: pinned, quiet markets with cheap far-month IV; pre-event
calendars additionally exploit near-term IV inflation (the weekly you sell is
richer than the monthly you own).
Primary risk: a big move away from the strike hurts in both directions — the
spread's value collapses toward zero at the wings; and the long leg's vega
cuts both ways (a far-month IV crush drains the position even when the spot
behaves).
India note: SEBI removed the calendar-spread margin benefit on expiry day —
the legs are margined as naked into the Tuesday close, so plan margin (and
exit) before the short leg's expiry, which this strategy enforces.
Backtest note: the synthetic backtest option provider prices both legs off a
single IV surface input, so the near/far IV differential that IS the calendar
edge will not show up in backtests — treat backtest P&L as plumbing
validation only.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volatility import realized_vol
from algobot.options.structures import calendar


class CalendarSpreadStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op11_calendar",
        name="Calendar Spread (weekly/monthly Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=40,
        params={
            # compendium suggests a tighter 0.15 rv ceiling; 0.22 keeps the
            # strategy tradeable in India's structurally higher-vol tape
            "max_rv": 0.22,            # 20-day realized vol ceiling (annualized fraction)
            "max_5d_ret_pct": 1.5,     # |5-day return| must be under this (pinned tape)
            "min_days_to_expiry": 3,   # enter early in the weekly cycle
            "move_exit_pct": 1.2,      # underlying move off entry that breaks the pin
            "exit_dte": 1,             # exit before the short leg's expiry day
            "max_hold_sessions": 4,    # dte-unavailable fallback time stop
        },
        capital_required=150_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Sell the Tuesday weekly ATM Nifty call, buy the monthly at "
                     "the same strike (net debit) in quiet, pinned regimes. Exit "
                     "on a >1.2% underlying move off entry or before the short "
                     "leg's expiry — never carry the naked-margined legs into "
                     "the Tuesday close."),
    )

    @staticmethod
    def _weekly_dte(sym: str, ctx: StrategyContext, fallback: int) -> int:
        try:
            from algobot.data.expiries import days_to_expiry
            from algobot.data.instruments import root_of
            return days_to_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
        except Exception:
            return fallback  # data layer unavailable (isolated backtest)

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        close = float(df.close.iloc[-1])
        p = self.params

        # ------------------------------------------------------------- exits
        if ctx.has_open_position:
            # both calendar legs belong to one structure — one EXIT closes it
            pos = ctx.open_positions[0]
            entry_ref = pos.underlying_entry if pos.underlying_entry else pos.avg_price
            # (a) big move away from the strike hurts both directions
            if entry_ref and abs(close - entry_ref) / entry_ref > p["move_exit_pct"] / 100.0:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"pin broken: {abs(close - entry_ref) / entry_ref * 100:.2f}% "
                           f"> {p['move_exit_pct']:.1f}% off entry")]
            # (b) exit before the short leg expires (naked margin on expiry day)
            dte = self._weekly_dte(sym, ctx, fallback=-1)
            if dte >= 0:
                if dte <= p["exit_dte"]:
                    return [Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        reason=f"short-leg expiry near: dte {dte} <= {p['exit_dte']}")]
            else:
                # dte unavailable -> session-count time stop
                opened_date = pos.opened_at.date()
                sessions_held = int(sum(1 for ts in df.index[-40:] if ts.date() > opened_date))
                if sessions_held >= p["max_hold_sessions"]:
                    return [Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        reason=f"time stop: {sessions_held} sessions >= "
                               f"{p['max_hold_sessions']}")]
            return []

        # ------------------------------------------------------------- entry
        # quiet: 20-day realized vol under the ceiling
        rv = realized_vol(df.close, n=20)
        rv_now = rv.iloc[-1]
        if pd.isna(rv_now) or float(rv_now) > p["max_rv"]:
            return []
        rv_now = float(rv_now)

        # pinned: small 5-day drift
        ref5 = float(df.close.iloc[-6])
        ret5 = (close - ref5) / ref5 * 100.0
        if abs(ret5) >= p["max_5d_ret_pct"]:
            return []

        # cycle: enough weekly life left to harvest the theta differential
        dte = self._weekly_dte(sym, ctx, fallback=4)
        if dte < p["min_days_to_expiry"]:
            return []

        # sell the weekly ATM call, buy the monthly at the same strike
        structure = calendar(sym, OptionType.CE, StrikeRule.atm(0),
                             near=ExpiryRule.weekly(0), far=ExpiryRule.monthly(0))

        # net-debit, defined-risk structure: max loss is the debit paid,
        # so no underlying stop — exits come from generate_signals above.
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=None, structure=structure,
            tags={"rv20": rv_now, "ret5_pct": ret5, "dte": dte},
            reason=f"pinned tape: rv20 {rv_now * 100:.1f}% <= {p['max_rv'] * 100:.0f}%, "
                   f"|5d ret| {abs(ret5):.2f}% < {p['max_5d_ret_pct']:.1f}%, dte {dte}")]
