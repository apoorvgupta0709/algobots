"""5.7 Momentum Burst / News Scalps (with index confirmation).

"Trade the stock in play — only while the index agrees." Edge: news-driven
institutional order flow prints as a relative-volume burst (>= ~3x the rolling
20-bar mean) on the 5-min chart while price breaks a key level (the prior
session's high/low); joining that flow early in the session, with the Nifty
above/below its 20-bar EMA confirming the tape, captures the impulsive first
leg. Regime: needs a directional, news-alive open — dead/rangebound opens
produce no bursts and no signals. Primary risk: chasing the third leg of an
already-extended move, and spreads that widen exactly when you exit into the
fade. India note: execute the cash equity directly (single-stock options carry
~1.2% slippage; the compendium prefers the cash/futures route here). The index
frame is confirmation only and is never traded.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from algobot.core import universes
from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EVERY_5MIN, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema

# Only the prior session + the rolling-volume window matter; trimming keeps
# 20-symbol 5-min backtests linear instead of quadratic in history length.
TRIM_BARS = 160


class MomentumBurstStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="id07_momentum_burst",
        name="Momentum Burst / News Scalps",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        # NIFTY_INDEX is confirmation only — never traded; no index frame => no signals.
        instruments=["NIFTY50_UNIVERSE", "NIFTY_INDEX"],
        warmup_bars=75,
        params={
            # rv_mult: real NSE volume is heavy-tailed, so 3.0x the rolling mean
            # fires regularly live; uniform synthetic fixture volume rarely
            # reaches 3x — lower rv_mult via config when testing on synthetic data.
            "rv_mult": 3.0,
            "rv_window": 20,          # rolling mean-volume lookback (excl. current bar)
            "index_ema": 20,          # index 5-min EMA confirmation period
            "entry_start": "09:20",   # only the first ~90 minutes
            "entry_end": "10:45",
            "rr_target": 1.5,         # take profit ~1.5R; central ratchet trails
        },
        capital_required=100_000,
        max_positions=2,
        max_trades_per_day=3,
        intraday_squareoff=True,
        description=("Relative-volume burst (>=3x 20-bar mean) breaking the prior "
                     "session high/low, taken only while the Nifty 5-min close "
                     "agrees with its 20-bar EMA, first 90 minutes only. Stock is "
                     "traded in cash; stop under the trigger candle, ~1.5R target."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        p = self.params

        # perf: trim every frame first — only the prior session + rolling window matter
        data = {sym: df.iloc[-TRIM_BARS:] for sym, df in data.items()}

        # session window gate: only the first 90 minutes
        start = dt.time.fromisoformat(p["entry_start"])
        end = dt.time.fromisoformat(p["entry_end"])
        if not (start <= ctx.now.time() <= end):
            return []

        # index confirmation frame (never traded); no index => no signals
        idx_df = data.pop(universes.NIFTY, None)
        if idx_df is None or len(idx_df) < p["index_ema"] + 1:
            return []
        idx_close = float(idx_df.close.iloc[-1])
        idx_ema = float(ema(idx_df.close, p["index_ema"]).iloc[-1])
        long_ok = idx_close > idx_ema
        short_ok = idx_close < idx_ema
        if not (long_ok or short_ok):
            return []

        today = ctx.now.date()
        open_syms = {pos.symbol for pos in ctx.open_positions}
        slots = self.meta.max_positions - len(ctx.open_positions)
        rr = p["rr_target"]
        rv_win = int(p["rv_window"])

        signals: list[Signal] = []
        for sym, df in data.items():
            if slots <= 0:
                break
            if sym in open_syms or len(df) < rv_win + 2:
                continue

            dates = df.index.date
            day = df[dates == today]
            if day.empty or df.index[-1] != day.index[-1]:
                continue
            prior = df[dates < today]
            if prior.empty:
                continue
            prev_session = prior[prior.index.date == prior.index[-1].date()]
            prev_high = float(prev_session.high.max())
            prev_low = float(prev_session.low.min())

            close = float(df.close.iloc[-1])

            # relative-volume burst vs rolling mean EXCLUDING the current bar;
            # apply only when volume data exists (index-style candles carry none)
            cur_vol = float(df.volume.iloc[-1])
            mean_vol = float(df.volume.iloc[-(rv_win + 1):-1].mean())
            if cur_vol > 0 and mean_vol > 0:
                if cur_vol < p["rv_mult"] * mean_vol:
                    continue

            # only the FIRST close through the level today triggers
            prior_closes = day.close.iloc[:-1]

            if long_ok and close > prev_high and (prior_closes <= prev_high).all():
                stop = float(df.low.iloc[-1])          # under the trigger candle
                if stop >= close:
                    continue
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=close + rr * (close - stop),
                    product_type=ProductType.INTRADAY,
                    reason=(f"momentum burst long: close {close:.2f} > prior high "
                            f"{prev_high:.2f}, index above EMA")))
                slots -= 1
            elif short_ok and close < prev_low and (prior_closes >= prev_low).all():
                stop = float(df.high.iloc[-1])         # above the trigger candle
                if stop <= close:
                    continue
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=close - rr * (stop - close),
                    product_type=ProductType.INTRADAY,
                    reason=(f"momentum burst short: close {close:.2f} < prior low "
                            f"{prev_low:.2f}, index below EMA")))
                slots -= 1
        return signals
