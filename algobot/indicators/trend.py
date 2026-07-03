"""Trend indicators: moving averages, Supertrend, ADX, crossovers.

All functions are vectorized pandas (Supertrend's final-band recursion is the
one unavoidable loop, run on numpy arrays). Warmup rows are NaN and no
function ever looks ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from algobot.indicators.volatility import atr


def ema(s: pd.Series, n: int) -> pd.Series:
    """Exponential moving average (span ``n``); NaN for the first ``n-1`` rows."""
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average over ``n`` bars."""
    return s.rolling(n, min_periods=n).mean()


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Supertrend with the recursive final-band logic.

    Args:
        df: OHLC frame with ``high``/``low``/``close`` columns.
        period: ATR lookback (Wilder).
        mult: band multiplier.

    Returns:
        DataFrame with columns ``st`` (the supertrend line) and ``direction``
        (+1 in an uptrend, -1 in a downtrend, NaN during warmup).
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].to_numpy(dtype=float)

    atr_ = atr(df, period).to_numpy(dtype=float)
    hl2 = ((high + low) / 2.0).to_numpy(dtype=float)
    basic_ub = hl2 + mult * atr_
    basic_lb = hl2 - mult * atr_

    n = len(df)
    st = np.full(n, np.nan)
    direction = np.full(n, np.nan)
    fub = np.full(n, np.nan)  # final upper band
    flb = np.full(n, np.nan)  # final lower band

    valid = np.where(~np.isnan(basic_ub))[0]
    if len(valid) == 0:
        return pd.DataFrame({"st": st, "direction": direction}, index=df.index)

    i0 = valid[0]
    fub[i0], flb[i0] = basic_ub[i0], basic_lb[i0]
    direction[i0] = 1.0 if close[i0] >= hl2[i0] else -1.0
    st[i0] = flb[i0] if direction[i0] > 0 else fub[i0]

    for i in range(i0 + 1, n):
        # Recursive final bands: only ratchet in the trend direction unless price
        # closed beyond the prior band.
        if basic_ub[i] < fub[i - 1] or close[i - 1] > fub[i - 1]:
            fub[i] = basic_ub[i]
        else:
            fub[i] = fub[i - 1]
        if basic_lb[i] > flb[i - 1] or close[i - 1] < flb[i - 1]:
            flb[i] = basic_lb[i]
        else:
            flb[i] = flb[i - 1]

        if direction[i - 1] > 0:  # uptrend, riding the lower band
            if close[i] < flb[i]:
                direction[i], st[i] = -1.0, fub[i]
            else:
                direction[i], st[i] = 1.0, flb[i]
        else:  # downtrend, riding the upper band
            if close[i] > fub[i]:
                direction[i], st[i] = 1.0, flb[i]
            else:
                direction[i], st[i] = -1.0, fub[i]

    return pd.DataFrame({"st": st, "direction": direction}, index=df.index)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average Directional Index (Wilder smoothing), 0-100."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    up = high.diff()
    dn = -low.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    tr[prev_close.isna()] = np.nan  # first bar has no true range

    alpha = 1.0 / n
    atr_w = tr.ewm(alpha=alpha, adjust=False, min_periods=n).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=n).mean() / atr_w
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=n).mean() / atr_w

    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0.0, np.nan)
    dx = dx.fillna(0.0).where(di_sum.notna())  # 0 when both DI are 0, NaN in warmup
    return dx.ewm(alpha=alpha, adjust=False, min_periods=n).mean().rename("adx")


def crossover(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """+1 on the bar ``fast`` crosses above ``slow``, -1 on a cross below, else 0."""
    diff = fast - slow
    prev = diff.shift(1)
    out = pd.Series(0, index=fast.index, dtype=int)
    out[(diff > 0) & (prev <= 0)] = 1
    out[(diff < 0) & (prev >= 0)] = -1
    return out
