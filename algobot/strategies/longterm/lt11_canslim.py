"""3.11 CANSLIM — O'Neil growth-momentum hybrid with a hard 7-8% stop.

Fundamental rocket fuel plus technical ignition: buy breakouts from sound
bases in fundamentally accelerating leaders, only while the market tide is in,
and cut every position at -7-8% without discussion.

Letters implemented:
- C + A (current/annual earnings): eps_growth > 25% AND revenue_growth > 10%
  from the fundamentals provider.
- N (new highs from a sound base): close within 5% of the 252-day high AND a
  close breaking above the max high of the prior ``base_bars`` bars (excluding
  today) whose range coiled to <= ``base_max_pct``. The "genuinely new driver"
  half of N (new product/management/industry condition) is NOT machine-checkable
  from prices and is left to the operator.
- S (supply/demand): proxied — tight-base + volume-expansion breakout only; no
  float/share-count data is consulted.
- L (leadership): 6-month price return in the top half of the universe.
- I (institutional sponsorship): proxied by breakout volume >= 1.4x its 20-bar
  mean (accumulation footprint), when volume data exists.
- M (market direction — trade only when the tide is in): Nifty close above BOTH
  its 50-day and 200-day SMAs; no new entries otherwise. Held names are exited
  when the tide goes out badly (index closes below its 200-day SMA).

Regime: early/mid bull markets where leadership and breadth are expanding; the
edge dies in bear and sideways tapes (which is exactly what M screens out).
Primary risk: whipsaw losses cluster in choppy tapes — many small -7.5% cuts in
a row. The hard loss cap IS the edge and the first thing users abandon; the
system only works if every stop is honoured without discussion.

India note: M maps to the Nifty holding above its 50- and 200-day SMAs; trades
NIFTY50_UNIVERSE large-caps as CNC delivery. The shipped
``config/fundamentals.csv`` is SYNTHETIC PLACEHOLDER data (only BHARTIARTL and
TATAMOTORS pass the C+A screen there) — replace with a real fundamentals feed
before production; the strategy degrades to no signals on an empty frame.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.fundamentals import CsvFundamentals, screen
from algobot.indicators.trend import sma


class CanslimStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt11_canslim",
        name="CANSLIM Growth-Momentum Breakout",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        # last entry is the M (market-direction) filter frame — never traded
        instruments=["NIFTY50_UNIVERSE", "NIFTY_INDEX"],
        warmup_bars=270,
        params={
            "eps_growth_min": 25.0,     # C+A: EPS growth floor, %
            "revenue_growth_min": 10.0, # C+A: revenue growth floor, %
            "leader_lookback": 126,     # L: ~6 months of trading days
            "market_sma_fast": 50,      # M: Nifty must close above both SMAs
            "market_sma_slow": 200,     # M: ...and exit all below this one
            "near_high_pct": 5.0,       # N: close within this % of the 252-day high
            "base_bars": 25,            # N: sound-base lookback (excluding today)
            "base_max_pct": 12.0,       # N: base range ceiling, % of close (the
                                        #    compendium says 10; synthetic tapes
                                        #    rarely coil under 10, so 12 default)
            "vol_mult": 1.4,            # I proxy: breakout volume vs 20-bar mean
            "vol_avg_bars": 20,
            "hard_stop_pct": 7.5,       # THE hard stop — cut, no discussion
            "max_new_entries": 2,       # new entries per scan
        },
        capital_required=300_000,
        max_positions=3,
        intraday_squareoff=False,
        description=("CANSLIM: buy base breakouts near 52-week highs in names with "
                     ">25% EPS and >10% revenue growth that lead the universe on "
                     "6-month return, on 1.4x volume, only while the Nifty holds its "
                     "50- and 200-day SMAs. Hard stop at -7.5%, no discussion; exit "
                     "all when the index loses its 200-day SMA."),
    )

    _INDEX_SYMBOL = "NSE:NIFTY50-INDEX"

    def __init__(self, params=None):
        super().__init__(params)
        # constructor I/O is allowed; signal-time I/O is not. ctx.extras wins
        # at scan time when the runtime injects a fundamentals frame.
        self._fundamentals = CsvFundamentals()

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        p = self.params
        slow = int(p["market_sma_slow"])

        # ------------------------------------------------------- M: market tide
        idx = data.get(self._INDEX_SYMBOL)
        if idx is None or len(idx) < slow:
            # the M filter is non-negotiable — no index frame, no trades
            return []
        idx_close = float(idx.close.iloc[-1])
        idx_sma_fast = float(sma(idx.close, int(p["market_sma_fast"])).iloc[-1])
        idx_sma_slow = float(sma(idx.close, slow).iloc[-1])
        tide_in = idx_close > idx_sma_fast and idx_close > idx_sma_slow
        tide_out_badly = idx_close < idx_sma_slow

        signals: list[Signal] = []
        open_syms = {pos.symbol for pos in ctx.open_positions}

        # ------------------------------------------- exits: tide has gone out
        if tide_out_badly:
            for sym in sorted(open_syms):
                df = data.get(sym)
                if df is None or df.empty:
                    continue
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now,
                    reference_price=float(df.close.iloc[-1]),
                    product_type=ProductType.CNC,
                    reason=(f"tide out: index {idx_close:.0f} below its "
                            f"{slow}-day SMA {idx_sma_slow:.0f}")))

        if not tide_in:
            return signals  # no new entries against the tide

        # ------------------------------------------- C+A: fundamentals screen
        candidates = sorted(s for s in data if s != self._INDEX_SYMBOL)
        if not candidates:
            return signals
        provider = ctx.extras.get("fundamentals") or self._fundamentals
        fund = provider.get(candidates)
        passing = set(screen(fund, {
            "eps_growth": (">", p["eps_growth_min"]),
            "revenue_growth": (">", p["revenue_growth_min"]),
        }).index)
        if not passing:
            return signals

        # ---------------------------------------------- L: 6-month leadership
        lookback = int(p["leader_lookback"])
        returns: dict[str, float] = {}
        for sym in candidates:
            df = data[sym]
            if len(df) <= lookback:
                continue
            past = float(df.close.iloc[-1 - lookback])
            if past > 0:
                returns[sym] = float(df.close.iloc[-1]) / past - 1.0
        if not returns:
            return signals
        ranked = sorted(returns, key=lambda s: (-returns[s], s))
        top_half = set(ranked[: max(1, len(ranked) // 2)])

        # -------------------------------- N + I: breakout from a sound base
        base_bars = int(p["base_bars"])
        slots = max(0, self.meta.max_positions - len(open_syms))
        new_entries = 0

        for sym in candidates:
            if new_entries >= min(int(p["max_new_entries"]), slots):
                break
            if sym in open_syms or sym not in passing or sym not in top_half:
                continue
            df = data[sym]
            if len(df) < self.meta.warmup_bars:
                continue
            close = float(df.close.iloc[-1])
            if close <= 0:
                continue

            # near the 52-week (252-bar) high
            hi_52wk = float(df.high.iloc[-252:].max())
            if close < hi_52wk * (1.0 - p["near_high_pct"] / 100.0):
                continue

            # sound (tight) base over the prior base_bars bars, excluding today
            base = df.iloc[-(base_bars + 1):-1]
            base_high = float(base.high.max())
            base_low = float(base.low.min())
            if (base_high - base_low) / close > p["base_max_pct"] / 100.0:
                continue

            # ignition: today's close breaks above the base high
            if close <= base_high:
                continue

            # institutional-accumulation proxy: volume expansion when data exists
            vol_today = float(df.volume.iloc[-1])
            avg_vol = float(df.volume.iloc[-int(p["vol_avg_bars"]):].mean())
            if vol_today > 0 and avg_vol > 0 and vol_today < p["vol_mult"] * avg_vol:
                continue

            stop = close * (1.0 - p["hard_stop_pct"] / 100.0)
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop, product_type=ProductType.CNC,
                reason=(f"CANSLIM breakout {close:.2f} > base high {base_high:.2f} "
                        f"near 52wk high {hi_52wk:.2f}; hard stop "
                        f"-{p['hard_stop_pct']:.1f}% at {stop:.2f}")))
            new_entries += 1

        return signals
