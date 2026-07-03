"""Volatility indicators: ATR (Wilder), Bollinger bands, squeeze, realized vol."""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range with Wilder smoothing; NaN warmup, no lookahead."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    tr[prev_close.isna()] = np.nan  # no true range on the first bar
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().rename("atr")


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Bollinger bands.

    Returns:
        DataFrame with ``mid`` (SMA), ``upper``, ``lower`` and ``width``
        (band spread as a fraction of the mid line).
    """
    mid = s.rolling(n, min_periods=n).mean()
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    upper = mid + k * sd
    lower = mid - k * sd
    width = (upper - lower) / mid
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width})


def bb_squeeze(s: pd.Series, n: int = 20, k: float = 2.0, lookback: int = 120) -> pd.Series:
    """True where Bollinger band width sits at its rolling ``lookback`` minimum."""
    width = bollinger(s, n, k)["width"]
    roll_min = width.rolling(lookback, min_periods=lookback).min()
    return ((width <= roll_min * (1 + 1e-12)) & roll_min.notna()).rename("bb_squeeze")


def realized_vol(daily_close: pd.Series, n: int = 20) -> pd.Series:
    """Annualized close-to-close realized volatility from log returns (sqrt(252))."""
    log_ret = np.log(daily_close.astype(float) / daily_close.shift(1))
    return (log_ret.rolling(n, min_periods=n).std() * np.sqrt(TRADING_DAYS)).rename(
        "realized_vol"
    )
