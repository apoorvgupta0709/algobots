"""6.5 Short Straddle — the intraday 9:20 variant.

After the opening auction settles, sell both ATM legs (CE + PE at the money,
weekly expiry) with fixed stop-losses per leg and square off by 3:00-3:15 pm.
Edge: opening-auction IV settles by 9:20 and the day's theta is harvested on
statistically quiet sessions — small first bar, small overnight gap. Regime:
quiet sessions and post-event vol-crush days; wild opens are skipped because
trend days blow legs through their stops. Primary risk: a trend day blows one
leg through its stop and keeps going, and SL-M fills on illiquid strikes can
be terrible — stop reliability must be proven in paper first. India note: the
9:20 straddle is crowded on Tuesday Nifty weeklies; 9:20 SL-M slippage is real
and measurable — model it before believing any backtest.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, Side, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, SizeHint
from algobot.core.strategy import SCAN_0920_ONCE, StrategyBase, StrategyContext, StrategyMeta
from algobot.options.structures import straddle


class ShortStraddle920Strategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op05_short_straddle_920",
        name="Short Straddle 9:20 (intraday Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_0920_ONCE,
        instruments=["NIFTY_INDEX"],
        warmup_bars=10,
        params={
            "max_first_bar_pct": 0.45,  # skip if first 5-min bar range wider than this
            "max_gap_pct": 0.5,         # skip if |gap vs prior session close| exceeds this
            "band_pct": 0.35,           # underlying band as the per-leg stop proxy
            "skip_expiry_day": False,   # optionally stand aside on weekly expiry day
        },
        capital_required=500_000,       # naked short straddle margin
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=True,        # engine flattens at 15:15 (compendium 3:00-3:15)
        is_multi_leg=True,
        description=("Sell the ATM weekly straddle once at 09:20 on statistically "
                     "quiet opens (small first bar, small gap); an underlying band "
                     "of +/-0.35% proxies the per-leg stops and a breach on either "
                     "side exits the whole structure; squared off intraday at 15:15."),
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

        # first 5-min bar of the session must be statistically quiet
        first = day.iloc[0]
        close = float(day.close.iloc[-1])
        first_range_pct = (float(first.high) - float(first.low)) / close * 100
        if first_range_pct > p["max_first_bar_pct"]:
            return []

        # gap vs prior session close must be small (skip wild opens)
        prior = df[df.index.date < today]
        if prior.empty:
            return []
        prev_close = float(prior.close.iloc[-1])
        gap_pct = abs(float(first.open) - prev_close) / prev_close * 100
        if gap_pct > p["max_gap_pct"]:
            return []

        if p["skip_expiry_day"]:
            try:
                from algobot.data.expiries import days_to_expiry
                from algobot.data.instruments import root_of
                if days_to_expiry(root_of(sym), "weekly", on_date=today) == 0:
                    return []
            except Exception:
                pass  # data layer unavailable (isolated backtest) -> don't skip

        structure = straddle(sym, Side.SELL, expiry_rule=ExpiryRule.weekly())
        band = close * p["band_pct"] / 100

        # short-vol position: the underlying band proxies the per-leg stops;
        # a breach on either side exits the whole structure via the monitor.
        # Sizing: the 9:20 straddle trades one lot per leg — naked margin
        # (capital_required), not premium risk, is the binding constraint, and
        # the central credit sizer (risk_amt // margin, min 1) resolves to
        # exactly 1 lot at the required capital; SizeHint(qty=1) states that
        # explicitly so the structure fills at any capital allocation.
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=close + band, take_profit=close - band,
            structure=structure, size_hint=SizeHint(qty=1),
            tags={"first_bar_range_pct": first_range_pct, "gap_pct": gap_pct},
            reason=(f"9:20 straddle: first bar {first_range_pct:.2f}%, "
                    f"gap {gap_pct:.2f}%, band +/-{p['band_pct']:.2f}%"))]
