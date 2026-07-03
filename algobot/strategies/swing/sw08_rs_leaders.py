"""4.8 Relative-Strength Leaders — own what outperforms the index while the index is healthy.

Edge: relative strength surfaces the leaders weeks before the narratives do —
blended 3m/6m outperformance vs the Nifty ranks the tape's strongest names, and
we only buy the top decile on a tactical trigger (pullback to the 20-EMA or a
20-day breakout).
Regime: sector-led bull phases where leadership persists; new entries are gated
on the Nifty closing above its 50-day EMA.
Risk: leaders fall hardest in corrections — the index filter is the seatbelt
(no new buys below the 50-EMA; exit everything when the index closes >2% below
it), plus RS-decay exits when a holding slips below the universe median.
India note: positional cousin of the §5.7 intraday RS play; single-name swing
fills can slip meaningfully vs EOD closes — prefer liquid Nifty-50 names and
limit orders near the reference price.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema
from algobot.indicators.volatility import atr


class RSLeadersStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw08_rs_leaders",
        name="Relative-Strength Leaders",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        # last entry is the benchmark / market-filter frame — never traded
        instruments=["NIFTY50_UNIVERSE", "NIFTY_INDEX"],
        warmup_bars=140,
        params={
            "rs_short": 63,          # ~3 months of trading days
            "rs_long": 126,          # ~6 months
            "min_bars": 130,         # bars a stock needs to be rankable
            "market_ema": 50,        # index health filter
            "seatbelt_pct": 0.02,    # index >2% below its 50-EMA => exit all
            "trigger_ema": 20,       # pullback EMA
            "breakout_lookback": 20, # prior N-day high breakout
            "atr_period": 14,
            "atr_mult": 2.0,
            "max_new_entries": 2,
        },
        capital_required=150_000,
        max_positions=3,
        intraday_squareoff=False,
        description=("Buy top-decile blended 3m/6m relative-strength names on a "
                     "pullback to the 20-EMA or a 20-day breakout, only while the "
                     "Nifty holds its 50-EMA. Exit on RS decay below the universe "
                     "median; exit everything when the index closes >2% below the "
                     "50-EMA — leaders fall hardest in corrections."),
    )

    _INDEX_SYMBOL = "NSE:NIFTY50-INDEX"

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        p = self.params
        idx = data.get(self._INDEX_SYMBOL)
        if idx is None or len(idx) < p["min_bars"]:
            # benchmark frame absent (e.g. trimmed fixtures) — nothing to rank against
            return []

        idx_close = float(idx.close.iloc[-1])
        idx_ema = float(ema(idx.close, p["market_ema"]).iloc[-1])
        market_healthy = idx_close > idx_ema
        seatbelt = idx_close < idx_ema * (1.0 - p["seatbelt_pct"])
        idx_r3 = idx_close / float(idx.close.iloc[-1 - p["rs_short"]]) - 1.0
        idx_r6 = idx_close / float(idx.close.iloc[-1 - p["rs_long"]]) - 1.0

        # ------------------------------------------------------------- RS ranking
        rs: dict[str, float] = {}
        for sym, df in data.items():
            if sym == self._INDEX_SYMBOL or len(df) < p["min_bars"]:
                continue
            close = float(df.close.iloc[-1])
            r3 = close / float(df.close.iloc[-1 - p["rs_short"]]) - 1.0
            r6 = close / float(df.close.iloc[-1 - p["rs_long"]]) - 1.0
            rs[sym] = 0.5 * (r3 - idx_r3) + 0.5 * (r6 - idx_r6)
        if not rs:
            return []

        ranked = sorted(rs, key=lambda s: (-rs[s], s))  # deterministic tie-break by symbol
        rank_of = {sym: i + 1 for i, sym in enumerate(ranked)}
        median_rs = float(pd.Series(list(rs.values())).median())
        top_k = max(1, len(ranked) // 10)  # top decile: 2 of a ~20-name universe

        signals: list[Signal] = []
        open_syms = {pos.symbol for pos in ctx.open_positions}

        # ---------------------------------------------------------------- exits
        for sym in sorted(open_syms):
            df = data.get(sym)
            if df is None or df.empty:
                continue
            close = float(df.close.iloc[-1])
            if seatbelt:
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    product_type=ProductType.CNC,
                    reason=f"correction seatbelt: index {idx_close:.0f} is >2% below "
                           f"its {p['market_ema']}-EMA {idx_ema:.0f}"))
            elif sym in rs and rs[sym] < median_rs:
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    product_type=ProductType.CNC,
                    reason=f"RS decay: rank {rank_of[sym]}/{len(ranked)} below "
                           f"universe median"))

        # --------------------------------------------------------------- entries
        if not market_healthy:
            return signals
        new_entries = 0
        slots = self.meta.max_positions - len(open_syms)
        for sym in ranked[:top_k]:
            if sym in open_syms or new_entries >= min(p["max_new_entries"], slots):
                continue
            df = data[sym]
            close = float(df.close.iloc[-1])
            low = float(df.low.iloc[-1])
            trig_ema = float(ema(df.close, p["trigger_ema"]).iloc[-1])
            prior_high = float(df.high.iloc[-1 - p["breakout_lookback"]:-1].max())
            pullback = low <= trig_ema and close > trig_ema
            breakout = close > prior_high
            if not (pullback or breakout):
                continue
            atr_now = float(atr(df, n=p["atr_period"]).iloc[-1])
            if not atr_now > 0:
                continue
            trigger = "pullback held 20-EMA" if pullback else "20-day breakout"
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=close - p["atr_mult"] * atr_now,
                product_type=ProductType.CNC,
                reason=f"RS rank {rank_of[sym]}/{len(ranked)} (blended 3m/6m vs "
                       f"Nifty), {trigger}, index above {p['market_ema']}-EMA"))
            new_entries += 1
        return signals
