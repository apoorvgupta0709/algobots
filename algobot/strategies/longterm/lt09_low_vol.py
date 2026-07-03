"""3.9 Low-Volatility Factor — the boring anomaly.

Edge: steadier stocks have historically delivered better risk-adjusted
returns than the market; holding the lowest-volatility large caps harvests
that premium. Regime: shines in choppy, falling or expensive markets — this
is the stabiliser sleeve of a portfolio. Risk: lags badly in roaring bull
markets, and crowding into low-vol names compresses the premium. India note:
pairs naturally with the momentum sleeve (§3.8) — together they form a
robust two-factor core.

Holds the ``top_n`` lowest 252-day realized-vol names from the large-cap
universe, equal weight, rebalanced semi-annually (April and October scans;
the strategy also acts on its first scan when nothing is held yet). Held
names are only rotated out when they fall outside the ``buffer_rank``
lowest-vol names, which curbs turnover. No stops — the factor itself is the
risk management.
"""
from __future__ import annotations

import math

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volatility import realized_vol


class LowVolFactorStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt09_low_vol",
        name="Low-Volatility Factor",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=270,
        params={
            "top_n": 5,                    # names held, equal weight
            "buffer_rank": 8,              # exit only when rank falls beyond this
            "vol_lookback": 252,           # trading days of realized vol
            "rebalance_months": [4, 10],   # semi-annual action gate (Apr/Oct)
        },
        capital_required=300_000,
        max_positions=1000,                # portfolio sleeve: not position-capped
        max_trades_per_day=1000,
        intraday_squareoff=False,
        description=("Hold the lowest 252-day realized-vol large caps equal weight, "
                     "rebalanced semi-annually. Edge: better risk-adjusted returns from "
                     "steady names; the stabiliser sleeve for choppy/falling/expensive "
                     "markets. Risk: lags badly in roaring bulls; crowding compresses "
                     "the premium."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        # Semi-annual gate: act on April/October scans, or on the first
        # deployment when nothing is held yet.
        if ctx.now.month not in self.params["rebalance_months"] and ctx.has_open_position:
            return []

        lookback = int(self.params["vol_lookback"])
        vols: dict[str, float] = {}
        closes: dict[str, float] = {}
        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue  # short history degrades out of the ranking
            vol = float(realized_vol(df.close, n=lookback).iloc[-1])
            if math.isnan(vol) or vol <= 0:
                continue
            vols[sym] = vol
            closes[sym] = float(df.close.iloc[-1])

        if not vols:
            return []

        ranked = sorted(vols, key=vols.get)  # ascending: steadiest first
        top_n = int(self.params["top_n"])
        selections = ranked[:top_n]
        buffer_set = set(ranked[: int(self.params["buffer_rank"])])
        held = {p.symbol for p in ctx.open_positions}

        signals: list[Signal] = []
        # Rotate out held names that fell outside the low-vol buffer (or lost
        # their ranking entirely, e.g. history no longer sufficient).
        for pos in ctx.open_positions:
            if pos.symbol in buffer_set:
                continue
            df = data.get(pos.symbol)
            last = float(df.close.iloc[-1]) if df is not None and len(df) else pos.avg_price
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                instrument=pos.symbol, timestamp=ctx.now, reference_price=last,
                product_type=ProductType.CNC,
                reason=(f"rotated out: vol rank beyond buffer "
                        f"({vols.get(pos.symbol, float('nan')):.3f} ann. vol)")))

        # Buy unheld selections at equal weight. No stop — the factor IS the
        # risk management for this sleeve.
        for sym in selections:
            if sym in held:
                continue
            rank = ranked.index(sym) + 1
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                instrument=sym, timestamp=ctx.now, reference_price=closes[sym],
                size_hint=SizeHint(weight=1.0 / top_n),
                product_type=ProductType.CNC,
                reason=f"low-vol rank {rank}/{len(ranked)}: {vols[sym]:.3f} ann. vol"))
        return signals
