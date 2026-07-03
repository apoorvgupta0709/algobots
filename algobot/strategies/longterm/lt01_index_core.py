"""3.1 Passive Index Core — own the market cheaply.

Own the market cheaply; let costs and taxes stay near zero. The equity core
goes into index ETFs — Nifty 50 (NIFTYBEES) for large-cap beta, Nifty Next 50
(JUNIORBEES) for breadth — at a 70/30 split. Review nothing except
once-a-year rebalancing.

Edge: this is the benchmark every other idea on the platform must beat after
costs and taxes. Most active approaches don't, so the passive core is the
default home for capital, not a fallback.

Risk: full, undiluted equity beta. 2008 was -55% for the index; the cure is
allocation (how much of the portfolio sits in this core), not exit. There are
deliberately no stops and no exit signals here.

Deployment is accumulate-only: each monthly scan deploys a capped slice of
uninvested capital into whichever ETF sits furthest below its target weight,
so drift is corrected with fresh purchases rather than sales. Sell-side
rebalancing is left to the operator once a year, because selling realises
LTCG — buying towards target keeps the tax drag at zero. Once the allocation
is fully invested (uninvested < 2% of capital) the strategy emits nothing.

India note: direct index funds run at <0.25% expense ratios; for the ETF
route, check iNAV spreads before placing market orders — NIFTYBEES and
JUNIORBEES usually trade tight, but wide spreads quietly tax every instalment.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta

CORE_ETF = "NSE:NIFTYBEES-EQ"
NEXT50_ETF = "NSE:JUNIORBEES-EQ"


class PassiveIndexCore(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt01_index_core",
        name="Passive Index Core",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=[CORE_ETF, NEXT50_ETF],
        warmup_bars=1,
        params={
            "core_weight": 0.70,       # NIFTYBEES target weight of deployed capital
            "next50_weight": 0.30,     # JUNIORBEES target weight of deployed capital
            "max_deploy_frac": 0.25,   # per-scan cap: fraction of capital deployable
            "fully_invested_pct": 0.02,  # stop buying once uninvested < 2% of capital
        },
        capital_required=200_000,
        max_positions=1000,            # accumulation strategy: positions never capped
        max_trades_per_day=2,
        intraday_squareoff=False,
        description=("70/30 Nifty 50 / Nifty Next 50 ETF core, accumulated monthly "
                     "into whichever ETF is furthest below target weight. Buy-only "
                     "(tax-efficient); annual sell-side rebalance is the operator's "
                     "move. The benchmark every other strategy must beat."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        targets = {
            CORE_ETF: float(self.params["core_weight"]),
            NEXT50_ETF: float(self.params["next50_weight"]),
        }

        # Latest closes for the two ETFs; without prices we cannot value holdings.
        closes: dict[str, float] = {}
        for sym in targets:
            df = data.get(sym)
            if df is not None and len(df) >= self.meta.warmup_bars:
                closes[sym] = float(df.close.iloc[-1])
        if not closes:
            return []

        # Current holdings value per ETF (qty * latest close; zero if not held)
        # and uninvested capital at cost, floored at zero.
        held_qty: dict[str, int] = {sym: 0 for sym in targets}
        cost_deployed = 0.0
        for pos in ctx.open_positions:
            if pos.symbol in held_qty:
                held_qty[pos.symbol] += pos.qty
                cost_deployed += pos.qty * pos.avg_price
        holdings = {sym: held_qty[sym] * closes.get(sym, 0.0) for sym in targets}

        capital = float(ctx.capital_allocated)
        uninvested = max(0.0, capital - cost_deployed)

        # Fully invested: nothing to do. Annual sell-side rebalancing is the
        # operator's move (selling realises LTCG; buying towards target doesn't).
        if uninvested < capital * float(self.params["fully_invested_pct"]):
            return []

        # Accumulate-only: deploy a capped slice into whichever ETF is
        # furthest below its target weight of the deployed total.
        slice_notional = min(uninvested, capital * float(self.params["max_deploy_frac"]))
        deployed_total = sum(holdings.values()) + slice_notional
        deficits = {
            sym: targets[sym] * deployed_total - holdings[sym]
            for sym in targets if sym in closes
        }
        buy_sym = max(deficits, key=deficits.get)

        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
            instrument=buy_sym, timestamp=ctx.now,
            reference_price=closes[buy_sym],
            size_hint=SizeHint(notional=slice_notional),
            product_type=ProductType.CNC,
            reason=(f"index-core accumulation: {buy_sym} "
                    f"{holdings[buy_sym] / deployed_total:.1%} held vs "
                    f"{targets[buy_sym]:.0%} target"))]
