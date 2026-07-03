"""Data-feed abstraction: candles + quotes with one canonical DataFrame shape.

Every feed (live Fyers, cached, synthetic backtest) returns candles in the
same canonical form so strategies/backtester/indicators never re-normalize:

* tz-aware ``Asia/Kolkata`` DatetimeIndex named ``ts``
* columns exactly ``[open, high, low, close, volume]`` (float)
* sorted ascending, no duplicate index entries
"""
from __future__ import annotations

import datetime as dt
import logging
from abc import ABC, abstractmethod

import pandas as pd

from algobot.core.enums import Timeframe
from algobot.core.exceptions import DataError

logger = logging.getLogger(__name__)

TZ = "Asia/Kolkata"
CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]


def empty_candles() -> pd.DataFrame:
    """An empty DataFrame in the canonical candle shape."""
    idx = pd.DatetimeIndex([], tz=TZ, name="ts")
    return pd.DataFrame({c: pd.Series(dtype=float) for c in CANDLE_COLUMNS}, index=idx)


def normalize_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce ``df`` into the canonical candle shape (non-mutating).

    Accepts a DatetimeIndex or a ``ts`` column (datetime-like). Naive
    timestamps are assumed to already be IST wall-clock; aware ones are
    converted. Duplicate timestamps are dropped keeping the *last* row
    (newest wins), and rows are sorted ascending.
    """
    if df is None:
        return empty_candles()
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        if "ts" in out.columns:
            out = out.set_index("ts")
        elif out.empty:
            return empty_candles()
        else:
            raise DataError("candles need a DatetimeIndex or a 'ts' column")
    if out.empty:
        return empty_candles()

    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    idx = idx.tz_localize(TZ) if idx.tz is None else idx.tz_convert(TZ)
    out.index = idx
    out.index.name = "ts"

    missing = [c for c in CANDLE_COLUMNS if c not in out.columns]
    if missing:
        raise DataError(f"candles missing columns: {missing}")
    out = out[CANDLE_COLUMNS].astype(float)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


class DataFeed(ABC):
    """Abstract market-data source (historical candles + last traded prices)."""

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: Timeframe,
                    start: dt.date, end: dt.date) -> pd.DataFrame:
        """Candles for ``symbol`` between ``start`` and ``end`` (both inclusive),
        in the canonical shape (see :func:`normalize_candles`)."""

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        """Last traded price per symbol."""
