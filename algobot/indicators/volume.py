"""Volume indicators: session-anchored VWAP, VWAP bands, relative volume.

Intraday frames must carry a tz-aware DatetimeIndex; sessions are keyed off
the index's local calendar date so VWAP resets at the start of every trading
day.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _session_key(df: pd.DataFrame) -> np.ndarray:
    """Local calendar date of each bar (tz-aware index required for intraday)."""
    return np.asarray(df.index.date)


def _typical_price(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"] + df["close"]) / 3.0


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP, reset each trading day.

    Cumulative typical-price*volume over cumulative volume within a session.
    When cumulative volume is zero (index data feeds report zero volume) the
    equal-weight expanding mean of typical price is used instead.
    """
    day = _session_key(df)
    tp = _typical_price(df).astype(float)
    vol = df["volume"].astype(float).fillna(0.0)

    cum_pv = (tp * vol).groupby(day).cumsum()
    cum_v = vol.groupby(day).cumsum()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = cum_pv / cum_v
    equal_weight = tp.groupby(day).transform(lambda s: s.expanding().mean())
    return out.where(cum_v > 0, equal_weight).rename("vwap")


def vwap_bands(df: pd.DataFrame, k: float = 1.5) -> pd.DataFrame:
    """VWAP with bands at +/- k times the per-session expanding std of
    (typical price - vwap). First bar of each session is NaN (no dispersion yet).
    """
    v = vwap(df)
    day = _session_key(df)
    dev = (_typical_price(df) - v).astype(float)
    sd = dev.groupby(day).transform(lambda s: s.expanding().std())
    return pd.DataFrame({"vwap": v, "upper": v + k * sd, "lower": v - k * sd})


def relative_volume(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Current bar volume over the trailing ``n``-bar average volume.

    The baseline excludes the current bar (shifted by one) so a spike does not
    dilute its own benchmark.
    """
    vol = df["volume"].astype(float)
    baseline = vol.shift(1).rolling(n, min_periods=n).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = vol / baseline.replace(0.0, np.nan)
    return out.rename("relative_volume")
