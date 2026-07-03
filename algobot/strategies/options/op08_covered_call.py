"""6.8 Covered Call — rent out upside you'd sell anyway.

Hold the stock, sell monthly calls against it. Against each delivered
lot-equivalent, sell a monthly 3-7% OTM call (~0.2-0.3 delta), targeting
1-2% of the stock value per month in premium. Edge: harvests option premium
on upside you would have trimmed into anyway. Regime: sideways-to-mildly-
bullish quality holdings — the overlay underperforms in strong trends and
adds nothing in a crash. Primary risk: the short call caps the melt-up; in a
crash the collected premium is cosmetic — this is NOT a hedge, the stock's
downside is fully open. India note: writing one call requires a full F&O lot
of the underlying delivered in the demat, Rs 2-6L+ per liquid name
(RELIANCE ~Rs 3-4L); index-proxy versions need Rs 16L+ of basket stock.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, SizeHint, StrikeRule
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema
from algobot.options.structures import covered_call


class CoveredCallStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op08_covered_call",
        name="Covered Call (monthly overlay)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NSE:RELIANCE-EQ"],
        warmup_bars=60,
        params={
            "stock_qty": 250,      # one F&O lot-equivalent of the underlying
            "otm_pct": 5.0,        # monthly call written 3-7% OTM (~0.2-0.3 delta)
        },
        capital_required=600_000,
        max_positions=1000,        # the stock base accumulates; never capped
        max_trades_per_day=2,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Hold a lot-equivalent of a liquid F&O stock and sell a "
                     "monthly ~5% OTM call against it, targeting 1-2% of stock "
                     "value per month in premium. Works in sideways-to-mildly-"
                     "bullish tape; caps the melt-up and is no hedge in a crash."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        p = self.params
        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue
            close = float(df.close.iloc[-1])

            stock_held = any(pos.symbol == sym for pos in ctx.open_positions)
            option_open = any(pos.symbol != sym for pos in ctx.open_positions)

            # ---- (1) stock leg: establish the delivered holding once
            if not stock_held:
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    size_hint=SizeHint(qty=int(p["stock_qty"])),
                    product_type=ProductType.CNC,
                    reason="covered-call stock base"))
                continue

            # ---- (2) call leg: one monthly short call against the holding
            if option_open:
                continue

            # regime filter: mildly bullish to sideways, not in a crash
            ema50 = ema(df.close, 50)
            if pd.isna(ema50.iloc[-1]) or close <= float(ema50.iloc[-1]) * 0.97:
                continue

            structure = covered_call(sym, StrikeRule.pct_otm(p["otm_pct"]),
                                     expiry_rule=ExpiryRule.monthly())

            # underlying-level stop ABOVE the short strike: exit the call on a
            # melt-up breach before assignment pain. Resolve the pct-OTM strike
            # against the chain's strike grid; fall back to close * 1.05.
            strike = None
            if ctx.option_chain is not None:
                try:
                    chain = ctx.option_chain(sym)
                    step = float(getattr(chain, "step", 0) or
                                 getattr(chain, "_step", 0) or 0)
                    if step > 0:
                        raw = close * (1 + p["otm_pct"] / 100.0)
                        strike = round(raw / step) * step
                except Exception:
                    strike = None
            if strike is None:
                strike = close * 1.05

            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=float(strike) * 1.02,
                structure=structure,
                tags={"short_call_strike": float(strike)},
                reason=f"monthly covered call: {p['otm_pct']:.0f}% OTM strike {strike:.0f}"))
        return signals
