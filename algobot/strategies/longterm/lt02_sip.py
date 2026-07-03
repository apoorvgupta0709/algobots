"""3.2 SIP / Rupee-Cost Averaging — reference long-term strategy.

Automate buying so behaviour cannot sabotage compounding: a fixed monthly
purchase of the index core, sized to savings rate rather than market view,
plus an optional lump-sum top-up rule on 10%+ index corrections.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta


class SIPStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt02_sip",
        name="SIP / Rupee-Cost Averaging",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NSE:NIFTYBEES-EQ"],
        warmup_bars=1,
        params={
            "monthly_amount": 25_000,
            "dip_topup_pct": 10.0,     # top-up when price is this % below 52wk high
            "dip_topup_amount": 25_000,
        },
        capital_required=300_000,
        max_positions=1000,            # accumulation strategy: positions never capped
        max_trades_per_day=2,
        intraday_squareoff=False,
        description=("Fixed monthly purchase of the index ETF on the first trading "
                     "day, with a lump-sum top-up on 10%+ corrections. The edge is "
                     "behavioural: it keeps buying exactly when manual investing stops."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        for sym, df in data.items():
            close = float(df.close.iloc[-1])
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                size_hint=SizeHint(notional=float(self.params["monthly_amount"])),
                product_type=ProductType.CNC,
                reason="monthly SIP instalment"))

            # dip top-up: price >=10% below the trailing 52-week high
            if len(df) >= 200:
                high_52wk = float(df.close.rolling(252, min_periods=200).max().iloc[-1])
                drawdown_pct = (high_52wk - close) / high_52wk * 100
                if drawdown_pct >= self.params["dip_topup_pct"]:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        size_hint=SizeHint(notional=float(self.params["dip_topup_amount"])),
                        product_type=ProductType.CNC,
                        reason=f"correction top-up: {drawdown_pct:.1f}% below 52wk high"))
        return signals
