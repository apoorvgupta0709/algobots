"""Momentum indicators: RSI (Wilder), ROC, 12-1 momentum, relative strength."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
TRADING_DAYS_PER_MONTH = 21


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Relative Strength Index with Wilder smoothing.

    Edge cases: after warmup, a series with zero average loss reads 100, zero
    average gain reads 0, and a perfectly flat series (both zero) reads a
    neutral 50 instead of NaN.
    """
    delta = s.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    alpha = 1.0 / n
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=n).mean()

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss  # inf when avg_loss == 0 -> RSI 100
        out = 100.0 - 100.0 / (1.0 + rs)
    flat = (avg_gain == 0) & (avg_loss == 0) & avg_gain.notna()
    return out.mask(flat, 50.0).rename("rsi")


def roc(s: pd.Series, n: int) -> pd.Series:
    """Rate of change over ``n`` bars, in percent."""
    return (s / s.shift(n) - 1.0) * 100.0


def momentum_12_1(daily_close: pd.Series) -> float:
    """Classic 12-1 cross-sectional momentum: 12-month return excluding the
    last month, using the 252/21 trading-day convention.

    Returns NaN when history is shorter than 252 bars or endpoints are NaN.
    """
    c = daily_close.astype(float)
    if len(c) < TRADING_DAYS_PER_YEAR:
        return float("nan")
    end = c.iloc[-TRADING_DAYS_PER_MONTH]
    start = c.iloc[-TRADING_DAYS_PER_YEAR]
    if pd.isna(end) or pd.isna(start) or start == 0:
        return float("nan")
    return float(end / start - 1.0)


def relative_strength(stock_close: pd.Series, index_close: pd.Series, n: int) -> pd.Series:
    """Stock return minus benchmark return over ``n`` bars (ratio-of-returns form).

    Positive values mean the stock outperformed the index over the window.
    """
    idx = index_close.reindex(stock_close.index)
    stock_ret = stock_close / stock_close.shift(n)
    index_ret = idx / idx.shift(n)
    return (stock_ret / index_ret - 1.0).rename("relative_strength")
