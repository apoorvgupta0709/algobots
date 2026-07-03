"""3.13 Sector Rotation — lean toward where the cycle sends earnings next.

Edge: sector leadership in India is persistent enough over months that
riding the leading sectors — early-cycle rotations into banks and autos
have paid best — beats holding the whole market. Regime: needs cyclical
turning points; when one sector regime persists for years the rotation
adds little over just holding it. Primary risk: being early twice —
leaving a trend that persists, and entering one that has not started.
India note: policy is a first-class cycle input — budgets, PLI schemes
and the RBI's stance start and end Indian sector trends; the 6-month
relative-strength rank used here proxies that clock.

Implementation note: the compendium's macro dashboard (credit growth,
PMI, rates) is not machine-fed, so this strategy uses the compendium's
own simpler proxy — hold the top ``top_n`` sector indices by 6-month
(126-bar) relative strength. Sector INDEX frames are the ranking signal
only and are never traded; execution is via each sector's designated
LEADER stock (``sector_leaders``). NIFTYREALTY is omitted because its
leader is not in the liquid trading universe. Held leaders are exited
only when their sector's rank falls below ``exit_rank`` — the monthly
cadence plus that rank buffer is the "rotate on regime change, not
headlines" discipline. No stops: rotation itself is the risk control.
"""
from __future__ import annotations

import math

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta


class SectorRotationStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt13_sector_rotation",
        name="Sector Rotation",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["SECTOR_UNIVERSE", "NIFTY50_UNIVERSE"],
        warmup_bars=140,
        params={
            "rs_lookback": 126,   # ~6 months of trading days
            "top_n": 2,           # sectors held via their leader stocks
            "exit_rank": 4,       # exit only when sector rank falls below this
            # Sector index -> leader stock traded for it (REALTY omitted:
            # its leader is not in the liquid universe).
            "sector_leaders": {
                "NSE:NIFTYBANK-INDEX": "NSE:HDFCBANK-EQ",
                "NSE:NIFTYIT-INDEX": "NSE:INFY-EQ",
                "NSE:NIFTYAUTO-INDEX": "NSE:MARUTI-EQ",
                "NSE:NIFTYPHARMA-INDEX": "NSE:SUNPHARMA-EQ",
                "NSE:NIFTYFMCG-INDEX": "NSE:ITC-EQ",
                "NSE:NIFTYMETAL-INDEX": "NSE:TATASTEEL-EQ",
                "NSE:NIFTYENERGY-INDEX": "NSE:RELIANCE-EQ",
            },
        },
        capital_required=300_000,
        max_positions=1000,        # portfolio sleeve: not position-capped
        max_trades_per_day=1000,
        intraday_squareoff=False,
        description=("Hold the leader stocks of the top-2 sector indices by 6-month "
                     "relative strength, rebalanced monthly with an exit-rank buffer "
                     "(rotate on regime change, not headlines). Edge: persistent "
                     "sector leadership; early-cycle rotations into banks/autos have "
                     "paid best in India. Risk: being early twice — leaving a "
                     "persisting trend, entering one that hasn't started."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        lookback = int(self.params["rs_lookback"])
        top_n = int(self.params["top_n"])
        exit_rank = int(self.params["exit_rank"])
        leaders: dict[str, str] = dict(self.params["sector_leaders"])

        # Rank sector INDICES by 6-month return. Indices are the signal only —
        # never traded. History guards degrade short-history sectors out.
        returns: dict[str, float] = {}
        for sector, _leader in leaders.items():
            df = data.get(sector)
            if df is None or len(df) <= lookback:
                continue
            last = float(df.close.iloc[-1])
            base = float(df.close.iloc[-1 - lookback])
            if math.isnan(last) or math.isnan(base) or base <= 0:
                continue
            returns[sector] = last / base - 1.0

        if not returns:
            return []

        ranked = sorted(returns, key=returns.get, reverse=True)  # strongest first
        rank_of = {sector: i + 1 for i, sector in enumerate(ranked)}
        target_leaders = {leaders[s]: s for s in ranked[:top_n]}
        sector_of_leader = {stock: sector for sector, stock in leaders.items()}
        held = {p.symbol for p in ctx.open_positions}

        signals: list[Signal] = []

        # Rotation exits: a held leader leaves only when its sector's rank
        # falls below the exit_rank buffer (or drops out of the ranking) —
        # regime change, not headlines.
        for pos in ctx.open_positions:
            sector = sector_of_leader.get(pos.symbol)
            rank = rank_of.get(sector) if sector else None
            if rank is not None and rank <= exit_rank:
                continue
            df = data.get(pos.symbol)
            last = float(df.close.iloc[-1]) if df is not None and len(df) else pos.avg_price
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                instrument=pos.symbol, timestamp=ctx.now, reference_price=last,
                product_type=ProductType.CNC,
                reason="rotation: sector leadership lost"))

        # Buys: unheld leaders of the top-ranked sectors, equal weight.
        # No stop — rotation is the risk management for this sleeve.
        for stock, sector in target_leaders.items():
            if stock in held:
                continue
            df = data.get(stock)
            if df is None or not len(df):
                continue  # leader frame unavailable: skip, never trade the index
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                instrument=stock, timestamp=ctx.now,
                reference_price=float(df.close.iloc[-1]),
                size_hint=SizeHint(weight=1.0 / top_n),
                product_type=ProductType.CNC,
                reason=(f"sector RS rank {rank_of[sector]}/{len(ranked)}: {sector} "
                        f"6m return {returns[sector] * 100:+.1f}%")))
        return signals
