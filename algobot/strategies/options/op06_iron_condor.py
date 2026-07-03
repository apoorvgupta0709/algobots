"""6.6 Iron Condor — reference options strategy.

The strangle with a seatbelt: sell ~0.20-delta call and put, buy wings beyond
both shorts. Defined risk, margin-light (the realistic entry into index premium
selling for mid-sized accounts). Entered early in the weekly cycle in calm
regimes; underlying-level stops sit at the short strikes so a breach exits the
whole structure before max loss.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_0920_ONCE, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volatility import realized_vol
from algobot.options.structures import iron_condor


class IronCondorStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op06_iron_condor",
        name="Iron Condor (weekly Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_0920_ONCE,
        instruments=["NIFTY_INDEX"],
        warmup_bars=40,
        params={
            "short_delta": 0.20,
            "wing_steps": 4,
            "min_days_to_expiry": 3,   # enter early cycle, skip expiry-adjacent entries
            "max_days_to_expiry": 7,
            "max_rv20_pct": 18.0,      # stand aside in high realized-vol regimes
        },
        capital_required=150_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Sell ~0.20-delta strangle with wings 4 steps out on the "
                     "Tuesday Nifty weekly, entered 3-7 days out in calm regimes. "
                     "Underlying stops at the short strikes exit the structure "
                     "before max loss; expiry settlement otherwise."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        close = float(df.close.iloc[-1])
        p = self.params

        # regime filter: calm tape only
        rv = realized_vol(df.close, n=20)
        if pd.isna(rv.iloc[-1]) or float(rv.iloc[-1]) * 100 > p["max_rv20_pct"]:
            return []

        # cycle filter: enter only with 3-7 days to the weekly expiry
        try:
            from algobot.data.expiries import days_to_expiry
            from algobot.data.instruments import root_of
            dte = days_to_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
        except Exception:
            dte = 5  # data layer unavailable (isolated backtest) -> assume mid-cycle
        if not (p["min_days_to_expiry"] <= dte <= p["max_days_to_expiry"]):
            return []

        structure = iron_condor(sym, short_delta=p["short_delta"],
                                wing_steps=p["wing_steps"])

        # approximate short strikes for underlying-level stops via the chain
        sl_low = sl_high = None
        if ctx.option_chain is not None:
            try:
                from algobot.data.expiries import next_expiry
                from algobot.data.instruments import root_of
                chain = ctx.option_chain(sym)
                expiry = next_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
                sl_high = chain.strike_by_delta(p["short_delta"], "CE", expiry)
                sl_low = chain.strike_by_delta(p["short_delta"], "PE", expiry)
            except Exception:
                pass
        if sl_high is None or sl_low is None:
            move = close * 0.015
            sl_high, sl_low = close + move, close - move

        # short-vol position: treat the call-side breach as the stop for the
        # (delta-short-biased) structure; the monitor exits the whole structure.
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=float(sl_high), take_profit=float(sl_low),
            structure=structure,
            tags={"short_put_strike": float(sl_low), "short_call_strike": float(sl_high)},
            reason=f"condor entry: rv20 {float(rv.iloc[-1])*100:.1f}%, dte {dte}")]
