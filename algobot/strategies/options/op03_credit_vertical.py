"""6.3 Credit Vertical Spreads (bull put / bear call) — structure-backed theta.

Edge: sell a level you believe won't break, buy a wing behind it, collect
theta. The short strike sits at a level backed by structure — support for bull
puts, resistance for bear calls — so the trade wins on up, sideways, and even
mildly against, as long as the level holds into expiry.

Regime: range or slow grind with elevated IV; post-event IV deflation is the
sweet spot. Here the trigger is a trend pullback: an uptrend bouncing off the
20-EMA above a rising 50-EMA (bull put), or the mirror rejection in a
downtrend (bear call).

Primary risk: risk-2-to-make-1 — one breached short strike erases many
winners; gap risk straight through the short strike means the stop cannot
guarantee the planned loss. The structure level breaking (10-day low/high on
the underlying) is the exit — no take_profit, theta and expiry settlement do
the work.

India note: the maths rarely fits tight per-trade caps — the margin and
max-loss on index credit spreads are large relative to small accounts, which
should express direction via 6.2 debit spreads instead.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema
from algobot.options.structures import vertical_spread


class CreditVerticalStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op03_credit_vertical",
        name="Credit Vertical (bull put / bear call, weekly Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=80,
        params={
            "trend_ema_n": 50,      # regime EMA: above+rising = uptrend, below+falling = downtrend
            "pullback_ema_n": 20,   # the level the pullback must touch and reject
            "slope_bars": 5,        # 50-EMA slope check vs N bars ago
            "stop_n": 10,           # underlying stop at the 10-day low/high
            "short_delta": 0.25,    # sell the level
            "long_delta": 0.15,     # buy the wing behind it
            "min_dte": 2,           # expiry-cycle guard: roll to next weekly when closer
        },
        capital_required=200_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Sell a 0.25-delta weekly put spread (0.15-delta wing) when an "
                     "uptrend bounces off the 20-EMA above a rising 50-EMA; mirror "
                     "0.25-delta call credit spread on a 20-EMA rejection in a "
                     "downtrend. Stop on the underlying at the 10-day low/high — the "
                     "structure level breaking is the exit; no take profit, theta and "
                     "expiry settlement do the work."),
    )

    def _strikes_degenerate(self, ctx: StrategyContext, sym: str, opt_type: str,
                            puts: bool) -> bool:
        """Belt-and-braces delta-resolution guard.

        Resolve the short (0.25d) and long (0.15d) strikes on the live chain and
        require them positive and correctly ordered (wing further OTM than the
        short). Returns True — i.e. skip the entry — only when the chain resolves
        to a degenerate pair; an unavailable/erroring chain cannot veto.
        """
        if ctx.option_chain is None:
            return False
        p = self.params
        try:
            chain = ctx.option_chain(sym)
            try:
                from algobot.data.expiries import next_expiry
                from algobot.data.instruments import root_of
                expiry = next_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
            except Exception:
                expiry = None
            short_k = float(chain.strike_by_delta(float(p["short_delta"]), opt_type, expiry))
            long_k = float(chain.strike_by_delta(float(p["long_delta"]), opt_type, expiry))
        except Exception:
            return False  # chain unusable -> LegBuilder resolves at execution
        if short_k <= 0 or long_k <= 0:
            return True
        # puts: wing below the short; calls: wing above the short
        return not (long_k < short_k if puts else long_k > short_k)

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        p = self.params
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []

        close = float(df.close.iloc[-1])
        low = float(df.low.iloc[-1])
        high = float(df.high.iloc[-1])

        ema_trend = ema(df.close, int(p["trend_ema_n"]))
        ema_pull = ema(df.close, int(p["pullback_ema_n"]))
        sb = int(p["slope_bars"])
        if (pd.isna(ema_trend.iloc[-1]) or pd.isna(ema_trend.iloc[-1 - sb])
                or pd.isna(ema_pull.iloc[-1])):
            return []
        trend_now, trend_then = float(ema_trend.iloc[-1]), float(ema_trend.iloc[-1 - sb])
        pull_now = float(ema_pull.iloc[-1])

        # bull put: uptrend bounces off support (touched the 20-EMA, closed above)
        bull_put = (close > trend_now and trend_now > trend_then
                    and low <= pull_now and close > pull_now)
        # bear call mirror: downtrend rejects resistance (touched from below, closed below)
        bear_call = (close < trend_now and trend_now < trend_then
                     and high >= pull_now and close < pull_now)
        if not (bull_put or bear_call):
            return []

        # expiry-cycle guard: expiry-adjacent weeklies produce degenerate legs
        # at EOD -> roll to the next weekly when too close.
        try:
            from algobot.data.expiries import days_to_expiry
            from algobot.data.instruments import root_of
            dte = days_to_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
        except Exception:
            dte = 4  # data layer unavailable (isolated backtest) -> assume mid-cycle
        expiry_rule = ExpiryRule.weekly(1) if dte < int(p["min_dte"]) else ExpiryRule.weekly()

        n = int(p["stop_n"])
        if bull_put:
            stop = float(df.low.iloc[-n:].min())  # 10-day low = the structure level
            if stop >= close:
                return []
            if self._strikes_degenerate(ctx, sym, "PE", puts=True):
                return []
            return [Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop,  # no take_profit: theta + expiry settlement do the work
                structure=vertical_spread(
                    sym, OptionType.PE, "credit",
                    buy_rule=StrikeRule.delta(float(p["long_delta"])),
                    sell_rule=StrikeRule.delta(float(p["short_delta"])),
                    expiry_rule=expiry_rule),
                reason=(f"bull put: 20-EMA bounce (low {low:.1f} <= {pull_now:.1f} "
                        f"< close {close:.1f}) above rising 50-EMA, dte {dte}"))]

        stop = float(df.high.iloc[-n:].max())  # 10-day high = the structure level
        if stop <= close:
            return []
        if self._strikes_degenerate(ctx, sym, "CE", puts=False):
            return []
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=stop,  # no take_profit: theta + expiry settlement do the work
            structure=vertical_spread(
                sym, OptionType.CE, "credit",
                buy_rule=StrikeRule.delta(float(p["long_delta"])),
                sell_rule=StrikeRule.delta(float(p["short_delta"])),
                expiry_rule=expiry_rule),
            reason=(f"bear call: 20-EMA rejection (high {high:.1f} >= {pull_now:.1f} "
                    f"> close {close:.1f}) below falling 50-EMA, dte {dte}"))]
