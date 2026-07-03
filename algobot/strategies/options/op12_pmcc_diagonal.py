"""6.12 Diagonal / Poor Man's Covered Call (PMCC).

The covered call for people without Rs 16 lakh: a deep ITM long call
(~0.75 delta, far month) stands in for the stock, and near-dated OTM calls
are sold against it for income while the short strike stays above the long's
breakeven. Edge: harvests short-dated theta on top of a trending long-delta
position at a fraction of the stock's capital. Regime: grinding uptrends —
price above a rising 50-day EMA in calm realized vol; the long leg is held
only while the trend lasts. Primary risk: a sharp drop hits the long leg with
little offset from the small short premium, and the structure needs more
active management than a true covered call (rolling shorts, defending the
long). India note: capital drops from Rs 16L+ (one Nifty lot of "stock") to
the deep-ITM premium, roughly Rs 40-90k on Nifty — the realistic
income-on-trend structure for mid-sized accounts.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema
from algobot.indicators.volatility import realized_vol
from algobot.options.structures import diagonal_pmcc


class PmccDiagonalStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op12_pmcc_diagonal",
        name="Diagonal PMCC (Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=80,
        params={
            "trend_ema": 50,          # trend filter and exit line
            "slope_lookback": 10,     # 50-EMA must be rising vs this many bars ago
            "rv_n": 20,               # realized-vol window
            "max_rv": 0.18,           # don't buy the expensive leg in panic vol
            "long_delta": 0.75,       # deep ITM far-month call (stock substitute)
        },
        capital_required=150_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Buy a ~0.75-delta far-month Nifty call as a stock substitute "
                     "and sell near-dated OTM calls against it in grinding uptrends "
                     "(close above a rising 50-day EMA, calm realized vol). Exit the "
                     "whole diagonal when close drops below the 50-day EMA — the "
                     "trend break ends the thesis."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        p = self.params

        close = float(df.close.iloc[-1])
        ema_line = ema(df.close, p["trend_ema"])
        ema_now = ema_line.iloc[-1]
        ema_then = ema_line.iloc[-1 - p["slope_lookback"]]
        if pd.isna(ema_now) or pd.isna(ema_then):
            return []
        ema_now = float(ema_now)

        # exit: hold the long only while the trend lasts
        if ctx.has_open_position:
            if close < ema_now:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"trend break: close {close:.1f} < 50-EMA {ema_now:.1f}")]
            return []

        # entry: grinding uptrend in calm vol
        if close <= ema_now or ema_now <= float(ema_then):
            return []
        rv = realized_vol(df.close, n=p["rv_n"])
        if pd.isna(rv.iloc[-1]) or float(rv.iloc[-1]) > p["max_rv"]:
            return []

        structure = diagonal_pmcc(sym, long_delta=p["long_delta"])
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=ema_now * 0.99,  # underlying trend break = thesis over
            structure=structure,
            tags={"ema50": ema_now, "rv20": float(rv.iloc[-1])},
            reason=(f"pmcc entry: close {close:.1f} > rising 50-EMA {ema_now:.1f}, "
                    f"rv20 {float(rv.iloc[-1])*100:.1f}%"))]
