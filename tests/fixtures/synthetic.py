"""Deterministic synthetic OHLCV generators for backtesting.

Both generators are pure functions of their arguments (numpy Generator seeded
explicitly, fixed default end date) so tests and CLI runs are reproducible.
Timestamps are tz-aware IST; the last row of a frame is a *closed* bar.

Conventions:
- ``index_5min``  — NSE cash session bars labelled 09:15..15:25 (75 bars/day).
- ``equity_daily`` — one bar per trading day labelled at 15:30 IST (the close),
  so end-of-day scan schedules fire on the session's final bar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
DEFAULT_END = "2026-06-30"          # fixed anchor => fully deterministic frames


def _session_offsets() -> pd.TimedeltaIndex:
    """5-min bar labels 09:15..15:25 as offsets from midnight."""
    return pd.timedelta_range("9h15min", "15h25min", freq="5min")


def index_5min(days: int = 30, seed: int = 42, start_price: float = 24000.0,
               end_date: str = DEFAULT_END) -> pd.DataFrame:
    """Synthetic index 5-minute candles over NSE sessions (09:15-15:25 IST).

    Random-walk with mild drift, intraday volatility and overnight gaps.
    Volume is zero (NSE index candles carry no volume).
    """
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(end=end_date, periods=days)
    offsets = _session_offsets()
    n_bars = len(offsets)

    idx_parts, opens, highs, lows, closes = [], [], [], [], []
    prev_close = float(start_price)
    for day in sessions:
        day_ts = pd.Timestamp(day.date()).tz_localize(IST)
        idx_parts.append(day_ts + offsets)

        gap = rng.normal(0.0, 0.004)                    # overnight gap ~0.4%
        day_open = prev_close * (1.0 + gap)
        rets = rng.normal(2e-5, 6e-4, size=n_bars)      # drift + intraday vol
        c = day_open * np.exp(np.cumsum(rets))
        o = np.empty(n_bars)
        o[0] = day_open
        o[1:] = c[:-1]
        wick = np.abs(rng.normal(0.0, 4e-4, size=n_bars))
        h = np.maximum(o, c) * (1.0 + wick)
        low = np.minimum(o, c) * (1.0 - wick)

        opens.append(o); highs.append(h); lows.append(low); closes.append(c)
        prev_close = float(c[-1])

    index = idx_parts[0].append(idx_parts[1:]) if len(idx_parts) > 1 else idx_parts[0]
    index.name = "ts"
    df = pd.DataFrame({
        "open": np.round(np.concatenate(opens), 2),
        "high": np.round(np.concatenate(highs), 2),
        "low": np.round(np.concatenate(lows), 2),
        "close": np.round(np.concatenate(closes), 2),
        "volume": np.zeros(len(index), dtype=np.int64),
    }, index=index)
    return df


def equity_daily(days: int = 500, seed: int = 7, start_price: float = 800.0,
                 end_date: str = DEFAULT_END) -> pd.DataFrame:
    """Synthetic daily equity candles (one bar/trading day, labelled 15:30 IST).

    Gentle drift + ~0.9%/day volatility (rv20 around 14% annualised) and a
    plausible lognormal volume column.
    """
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(end=end_date, periods=days)
    index = pd.DatetimeIndex(
        [pd.Timestamp(d.year, d.month, d.day, 15, 30).tz_localize(IST)
         for d in sessions], name="ts")

    rets = rng.normal(3e-4, 9e-3, size=days)
    c = float(start_price) * np.exp(np.cumsum(rets))
    o = np.empty(days)
    o[0] = start_price
    o[1:] = c[:-1] * (1.0 + rng.normal(0.0, 3e-3, size=days - 1))
    wick = np.abs(rng.normal(0.0, 3e-3, size=days))
    h = np.maximum(o, c) * (1.0 + wick)
    low = np.minimum(o, c) * (1.0 - wick)
    volume = np.round(rng.lognormal(mean=13.0, sigma=0.4, size=days)).astype(np.int64)

    return pd.DataFrame({
        "open": np.round(o, 2), "high": np.round(h, 2),
        "low": np.round(low, 2), "close": np.round(c, 2),
        "volume": volume,
    }, index=index)
