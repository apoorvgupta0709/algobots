"""3.3 Asset Allocation and Rebalancing — 60/30/10 band rebalance.

The only free lunch: pre-committed selling high and buying low across assets.
Fix target weights — 60% equity (NIFTYBEES) / 30% debt (LIQUIDBEES) / 10% gold
(GOLDBEES) — and rebalance on a monthly calendar with a band trigger: act when
any sleeve drifts more than ``band_pp`` percentage points above its target.

Edge: the rules force equity buying in crashes (equity sleeve shrinks below
target, so fresh money and trim proceeds flow into it) and trimming in
euphoria (an over-run sleeve is cut back mechanically). Behavioural discipline
is encoded, not hoped for.

Primary risk: tax and exit-load friction if the bands are too tight — every
trim is a realised capital-gains event, which argues for wide bands and a slow
calendar. A second risk: rebalancing mechanically buys more of a structurally
dying asset; the free lunch assumes mean reversion between sleeves.

Implementation notes: sleeve trims are coarse — a breach exits the FULL
position and the next accumulation phase redeploys to targets. Partial trims
(selling only the excess) are a refinement; taxes argue for wide bands either
way. India note: multi-asset allocation funds outsource this same discipline
with better tax treatment — a fund's internal rebalancing is not a taxable
event for you, while DIY trims are.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta


class RebalanceStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt03_rebalance",
        name="Asset Allocation Rebalancing (60/30/10)",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NSE:NIFTYBEES-EQ", "NSE:LIQUIDBEES-EQ", "NSE:GOLDBEES-EQ"],
        warmup_bars=1,
        params={
            "target_weights": {
                "NSE:NIFTYBEES-EQ": 0.60,   # equity core
                "NSE:LIQUIDBEES-EQ": 0.30,  # debt / cash-equivalent
                "NSE:GOLDBEES-EQ": 0.10,    # gold
            },
            "band_pp": 5.0,                 # trim when a sleeve is this many pp above target
        },
        capital_required=300_000,
        max_positions=1000,                 # accumulation strategy: positions never capped
        max_trades_per_day=3,
        intraday_squareoff=False,
        description=("Fixed 60/30/10 equity/debt/gold targets, rebalanced monthly with a "
                     "5pp band: builds sleeves from cash, then trims any sleeve that "
                     "over-runs its target and redirects the excess to the most "
                     "underweight sleeve. Pre-committed sell-high/buy-low across assets."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        targets: dict[str, float] = self.params["target_weights"]
        band = float(self.params["band_pp"]) / 100.0
        capital = float(ctx.capital_allocated)

        # latest close per sleeve — all sleeves must be priced to compute weights
        closes: dict[str, float] = {}
        for sym in targets:
            df = data.get(sym)
            if df is None or len(df) < self.meta.warmup_bars:
                return []
            closes[sym] = float(df.close.iloc[-1])

        # sleeve values from open positions; uninvested = capital - cost basis
        qty = {sym: 0 for sym in targets}
        invested_cost = 0.0
        for pos in ctx.open_positions:
            if pos.symbol in qty:
                qty[pos.symbol] += pos.qty
                invested_cost += pos.qty * pos.avg_price
        sleeve = {sym: qty[sym] * closes[sym] for sym in targets}
        uninvested = max(capital - invested_cost, 0.0)
        total = sum(sleeve.values()) + uninvested
        if total <= 0:
            return []

        weights = {sym: sleeve[sym] / total for sym in targets}
        # most underweight sleeve; ties break deterministically in target order
        under_sym = max(targets, key=lambda s: targets[s] - weights[s])

        # PHASE 1 — building: deploy cash in tranches into the most underweight sleeve
        if uninvested > 0.05 * capital:
            deploy = min(uninvested, capital * 0.34)
            return [Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                instrument=under_sym, timestamp=ctx.now,
                reference_price=closes[under_sym],
                size_hint=SizeHint(notional=deploy),
                product_type=ProductType.CNC,
                reason=(f"accumulation tranche into most underweight sleeve "
                        f"({weights[under_sym]:.1%} vs {targets[under_sym]:.0%} target)"))]

        # PHASE 2 — maintaining: trim any sleeve more than band_pp above target
        signals: list[Signal] = []
        trimmed_excess = 0.0
        for sym in targets:
            drift = weights[sym] - targets[sym]
            if drift > band:
                trimmed_excess += sleeve[sym] - targets[sym] * total
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=closes[sym],
                    product_type=ProductType.CNC,
                    reason=(f"rebalance trim: {weights[sym]:.1%} vs "
                            f"{targets[sym]:.0%} target (+{drift * 100:.1f}pp)")))
        if signals and trimmed_excess > 0 and under_sym not in {s.instrument for s in signals}:
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                instrument=under_sym, timestamp=ctx.now,
                reference_price=closes[under_sym],
                size_hint=SizeHint(notional=trimmed_excess),
                product_type=ProductType.CNC,
                reason=(f"redeploy trimmed excess into most underweight sleeve "
                        f"({weights[under_sym]:.1%} vs {targets[under_sym]:.0%} target)")))
        return signals
