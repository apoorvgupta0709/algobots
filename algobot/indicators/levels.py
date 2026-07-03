"""Price levels: CPR, opening range, prior-day HLC, 52-week high, gaps, pivots.

Daily frames are expected to hold one row per completed session with
``open/high/low/close`` columns, oldest first.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd


def cpr(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """Central Pivot Range from the prior session's H/L/C.

    Returns ``{pivot, bc, tc, width_pct}`` with ``bc <= tc`` (the raw top/bottom
    central pivots are swapped when needed) and width as percent of the pivot.
    """
    pivot = (prev_high + prev_low + prev_close) / 3.0
    bc_raw = (prev_high + prev_low) / 2.0
    tc_raw = 2.0 * pivot - bc_raw
    bc, tc = min(bc_raw, tc_raw), max(bc_raw, tc_raw)
    width_pct = (tc - bc) / pivot * 100.0 if pivot else float("nan")
    return {"pivot": pivot, "bc": bc, "tc": tc, "width_pct": width_pct}


def daily_cpr(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized CPR for every row, computed from the PRIOR day (shift(1)).

    The first row is NaN — there is no prior session and we never look ahead.
    """
    ph = daily_df["high"].astype(float).shift(1)
    pl = daily_df["low"].astype(float).shift(1)
    pc = daily_df["close"].astype(float).shift(1)
    pivot = (ph + pl + pc) / 3.0
    bc_raw = (ph + pl) / 2.0
    tc_raw = 2.0 * pivot - bc_raw
    bc = np.minimum(bc_raw, tc_raw)
    tc = np.maximum(bc_raw, tc_raw)
    width_pct = (tc - bc) / pivot * 100.0
    return pd.DataFrame(
        {"pivot": pivot, "bc": bc, "tc": tc, "width_pct": width_pct}, index=daily_df.index
    )


def opening_range(intraday_df: pd.DataFrame, minutes: int = 30) -> dict:
    """High/low of the first ``minutes`` of the LATEST session in the frame.

    The range is anchored on the first bar of the last calendar date in the
    (tz-aware) index, so partial sessions work during live trading.
    """
    if intraday_df.empty:
        return {"high": float("nan"), "low": float("nan")}
    dates = np.asarray(intraday_df.index.date)
    last_day = dates[-1]
    session = intraday_df[dates == last_day]
    anchor = session.index[0]
    window = session[session.index < anchor + dt.timedelta(minutes=minutes)]
    return {"high": float(window["high"].max()), "low": float(window["low"].min())}


def prev_day_hlc(daily_df: pd.DataFrame) -> dict:
    """H/L/C of the last row of ``daily_df``.

    Callers must pass completed daily bars (i.e. history up to and including
    the previous session); the last row is therefore "yesterday".
    """
    if daily_df.empty:
        return {"high": float("nan"), "low": float("nan"), "close": float("nan")}
    row = daily_df.iloc[-1]
    return {"high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"])}


def high_52wk(daily_close: pd.Series) -> pd.Series:
    """Rolling 52-week (252 trading day) high including the current bar.

    Uses ``min_periods=1``: with shorter history the high over the available
    window is returned (a 52-week high is well-defined on partial history).
    """
    return daily_close.rolling(252, min_periods=1).max().rename("high_52wk")


def gap_pct(daily_df: pd.DataFrame) -> pd.Series:
    """Opening gap versus the prior close, in percent (NaN on the first row)."""
    prev_close = daily_df["close"].astype(float).shift(1)
    return ((daily_df["open"].astype(float) - prev_close) / prev_close * 100.0).rename(
        "gap_pct"
    )


def pivots_sr(daily_df: pd.DataFrame) -> dict:
    """Classic floor-trader pivots from the LAST completed row of ``daily_df``.

    Returns ``{pivot, r1, r2, s1, s2}``.
    """
    if daily_df.empty:
        nan = float("nan")
        return {"pivot": nan, "r1": nan, "r2": nan, "s1": nan, "s2": nan}
    row = daily_df.iloc[-1]
    h, l, c = float(row["high"]), float(row["low"]), float(row["close"])
    pivot = (h + l + c) / 3.0
    return {
        "pivot": pivot,
        "r1": 2.0 * pivot - l,
        "s1": 2.0 * pivot - h,
        "r2": pivot + (h - l),
        "s2": pivot - (h - l),
    }
