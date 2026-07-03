"""6.1 Long Call / Long Put — directional premium buying on breakouts.

Edge: maximum convexity, maximum rent — pay premium for unlimited upside and a
known worst case. Buy ~0.50-delta options in the trade direction on a momentum
breakout (20-day high/low break confirmed by the slope of the 20-EMA) and take
profits into strength, because theta punishes patience.

Regime: strong trends with rising IV; expiry-afternoon gamma is where long
premium pays disproportionately. Avoid buying into already-elevated IV — a
20-day realized-vol ceiling stands in as the IV filter.

Primary risk: being right on direction and still losing to time decay and IV
crush; whipsaw breakouts bleed premium fast.

India note: long options require the full premium upfront (no margin games).
Deep-OTM lottery buying is the retail loss engine — this stays near the money
(~0.50 delta) where the option actually tracks the move.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema
from algobot.indicators.volatility import realized_vol
from algobot.options.structures import long_option


class LongCallPutStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op01_long_call_put",
        name="Long Call / Long Put (Nifty breakout)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=60,
        params={
            "breakout_n": 20,       # prior high/low lookback
            "ema_n": 20,            # trend EMA
            "slope_bars": 5,        # EMA slope check vs N bars ago
            "stop_ema_n": 10,       # underlying stop at the 10-EMA
            "target_r": 2.0,        # take profit ~2R on the underlying
            "rv_max": 0.22,         # skip entries when 20d realized vol is elevated
            "delta": 0.50,          # near-the-money, not lottery tickets
            "min_dte": 1,           # expiry-cycle guard: roll to next weekly
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Buy a ~0.50-delta weekly CE on a 20-day-high break above a "
                     "rising 20-EMA (PE mirror on the low break below a falling "
                     "20-EMA), only when 20d realized vol is not elevated. Stop on "
                     "the underlying at the 10-EMA, take profit ~2R into strength."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        p = self.params
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []

        close = float(df.close.iloc[-1])
        n = int(p["breakout_n"])
        prior_high = float(df.high.iloc[-(n + 1):-1].max())
        prior_low = float(df.low.iloc[-(n + 1):-1].min())

        ema_trend = ema(df.close, int(p["ema_n"]))
        ema_stop = ema(df.close, int(p["stop_ema_n"]))
        sb = int(p["slope_bars"])
        if pd.isna(ema_trend.iloc[-1]) or pd.isna(ema_trend.iloc[-1 - sb]) or pd.isna(ema_stop.iloc[-1]):
            return []
        ema_now, ema_then = float(ema_trend.iloc[-1]), float(ema_trend.iloc[-1 - sb])
        stop_ref = float(ema_stop.iloc[-1])

        # vol filter: don't buy premium into an elevated-IV proxy
        rv = realized_vol(df.close, n=20)
        if pd.isna(rv.iloc[-1]) or float(rv.iloc[-1]) > p["rv_max"]:
            return []
        rv_now = float(rv.iloc[-1])

        long_call = close > prior_high and ema_now > ema_then
        long_put = close < prior_low and ema_now < ema_then
        if not (long_call or long_put):
            return []

        # expiry-cycle guard: same-day weekly expiries at EOD produce
        # degenerate legs -> roll to the next weekly when too close.
        try:
            from algobot.data.expiries import days_to_expiry
            from algobot.data.instruments import root_of
            dte = days_to_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
        except Exception:
            dte = 3  # data layer unavailable (isolated backtest) -> assume mid-cycle
        expiry_rule = ExpiryRule.weekly(1) if dte < int(p["min_dte"]) else ExpiryRule.weekly()

        strike_rule = StrikeRule.delta(float(p["delta"]))
        if long_call:
            if stop_ref >= close:
                return []  # stop must sit below a long entry
            risk = close - stop_ref
            return [Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop_ref, take_profit=close + p["target_r"] * risk,
                structure=long_option(sym, OptionType.CE, strike_rule,
                                      expiry_rule=expiry_rule),
                reason=(f"20d-high break {close:.1f} > {prior_high:.1f}, "
                        f"20-EMA rising, rv20 {rv_now:.2%}, dte {dte}"))]

        if stop_ref <= close:
            return []  # stop must sit above a short entry
        risk = stop_ref - close
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=stop_ref, take_profit=close - p["target_r"] * risk,
            structure=long_option(sym, OptionType.PE, strike_rule,
                                  expiry_rule=expiry_rule),
            reason=(f"20d-low break {close:.1f} < {prior_low:.1f}, "
                    f"20-EMA falling, rv20 {rv_now:.2%}, dte {dte}"))]
