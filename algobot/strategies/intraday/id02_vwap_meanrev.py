"""5.2 VWAP Mean Reversion — fade emotional stretches back to the day's
institutional fair value.

Edge: on non-trend days intraday price oscillates around session VWAP, the
institutional average cost; emotional pushes far from it tend to snap back.
Entry when price stretches beyond max(0.35% of the index, 1.5x the session's
expanding std of price-VWAP) AND the latest 5-min bar is a reversal candle
closing back toward VWAP. Target is the VWAP touch itself; stop beyond the
stretch extreme (recent 6-bar extreme past the entry). Max 2 fades a day.

Regime: needs a rotational / range session. Skipped when the day looks trendy
(price pinned to one side of VWAP for the entire session so far after the
first hour), during the 11:30-13:15 lunch chop, and outside 10:15-14:30.

Risk: trend days that never revert — the stretch keeps stretching; the stop
beyond the extreme caps it. Executed via ATM debit verticals so the premium
at risk is hard-capped.

India note: pairs with the CPR regime filter (narrow-CPR trend days are the
ones to skip); with small VWAP-touch targets, friction (STT, spread) eats a
large slice — take only clean stretches, never force the trade.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EVERY_5MIN, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volume import vwap
from algobot.options.structures import vertical_spread


class VWAPMeanReversion(StrategyBase):
    meta = StrategyMeta(
        strategy_id="id02_vwap_meanrev",
        name="VWAP Mean Reversion",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=["NIFTY_INDEX"],
        warmup_bars=12,
        params={
            "stretch_pct": 0.35,      # min stretch as % of index level
            "std_mult": 1.5,          # ... or this many session stds of (close - vwap)
            "stop_lookback": 6,       # bars for the stretch-extreme stop
            "first_entry": "10:15",   # no fades before the first hour settles
            "last_entry": "14:30",
            "lunch_start": "11:30",   # skip lunch chop
            "lunch_end": "13:15",
            "min_session_bars": 8,    # need dispersion history before trusting the std
            "spread_width_steps": 4,  # debit vertical width in strike steps
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=2,
        intraday_squareoff=True,
        is_multi_leg=True,
        description=("Fade stretches beyond max(0.35%, 1.5x session std) from session "
                     "VWAP on a reversal candle, targeting the VWAP touch, stop beyond "
                     "the 6-bar stretch extreme. Non-trend days only; max 2 fades/day."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []
        if ctx.trades_today >= self.meta.max_trades_per_day:
            return []

        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        today = ctx.now.date()
        day = df[df.index.date == today]
        if len(day) < int(self.params["min_session_bars"]):
            return []

        first_entry = dt.time.fromisoformat(self.params["first_entry"])
        last_entry = dt.time.fromisoformat(self.params["last_entry"])
        lunch_start = dt.time.fromisoformat(self.params["lunch_start"])
        lunch_end = dt.time.fromisoformat(self.params["lunch_end"])

        bar_time = day.index[-1].time()
        if not (first_entry <= bar_time <= last_entry):
            return []
        if lunch_start <= bar_time < lunch_end:
            return []

        v = vwap(day)  # session-anchored; equal-weight fallback for zero-volume index candles
        session_vwap = float(v.iloc[-1])
        closes = day.close.astype(float)

        # regime filter: price on one side of VWAP for the entire session so far
        # (we are past the first hour here) -> trend day, do not fade it
        if (closes > v).all() or (closes < v).all():
            return []

        # stretch threshold: % of index or session dispersion, whichever greater
        close = float(day.close.iloc[-1])
        bar_open = float(day.open.iloc[-1])
        sd = float((closes - v).expanding().std().iloc[-1])
        threshold = self.params["stretch_pct"] / 100.0 * close
        if pd.notna(sd):
            threshold = max(threshold, self.params["std_mult"] * sd)

        lookback = int(self.params["stop_lookback"])
        recent = day.iloc[-lookback:]
        width = int(self.params["spread_width_steps"])
        signals: list[Signal] = []

        stretched_below = session_vwap - float(day.low.iloc[-1]) >= threshold
        stretched_above = float(day.high.iloc[-1]) - session_vwap >= threshold

        if stretched_below and close < session_vwap and close > bar_open:
            # below-VWAP stretch + reversal candle -> fade up toward VWAP
            stop = float(recent.low.min())
            if stop < close:
                structure = vertical_spread(
                    sym, OptionType.CE, "debit",
                    buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(width),
                    expiry_rule=ExpiryRule.weekly())
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=session_vwap,
                    structure=structure,
                    reason=(f"VWAP fade long: stretch {session_vwap - close:.1f} below "
                            f"VWAP {session_vwap:.1f} (thr {threshold:.1f}), reversal bar")))
        elif stretched_above and close > session_vwap and close < bar_open:
            # above-VWAP stretch + reversal candle -> fade down toward VWAP
            stop = float(recent.high.max())
            if stop > close:
                structure = vertical_spread(
                    sym, OptionType.PE, "debit",
                    buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(-width),
                    expiry_rule=ExpiryRule.weekly())
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=session_vwap,
                    structure=structure,
                    reason=(f"VWAP fade short: stretch {close - session_vwap:.1f} above "
                            f"VWAP {session_vwap:.1f} (thr {threshold:.1f}), reversal bar")))
        return signals
