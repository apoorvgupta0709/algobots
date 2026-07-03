"""Parquet candle cache and a caching DataFeed wrapper.

``CandleCache`` stores one parquet file per (symbol, timeframe) under
``settings()['data_cache_dir']``. ``CachedFeed`` serves candles from the
cache and asks its inner feed only for the missing head/tail of a request,
merging fresh rows back into the cache (newest wins on overlap). With no
inner feed it runs cache-only (offline / backtest mode).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from pathlib import Path

import pandas as pd

from algobot.core.config import settings
from algobot.core.enums import Timeframe
from algobot.core.exceptions import DataError
from algobot.data.feed import TZ, DataFeed, normalize_candles

logger = logging.getLogger(__name__)


def _tf_key(timeframe: Timeframe | str) -> str:
    return timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)


def sanitize_symbol(symbol: str) -> str:
    """Filesystem-safe directory name for a Fyers symbol."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", symbol)


class CandleCache:
    """On-disk parquet store: ``{cache_dir}/{sanitized_symbol}/{timeframe}.parquet``."""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self.cache_dir = Path(cache_dir or settings()["data_cache_dir"])

    def path(self, symbol: str, timeframe: Timeframe | str) -> Path:
        return self.cache_dir / sanitize_symbol(symbol) / f"{_tf_key(timeframe)}.parquet"

    def read(self, symbol: str, timeframe: Timeframe | str) -> pd.DataFrame | None:
        path = self.path(symbol, timeframe)
        if not path.exists():
            return None
        try:
            return normalize_candles(pd.read_parquet(path))
        except Exception:
            logger.exception("unreadable cache file %s", path)
            return None

    def write(self, symbol: str, timeframe: Timeframe | str, df: pd.DataFrame) -> pd.DataFrame:
        """Merge ``df`` into the cache (dedupe on index, newest wins) and persist.

        Returns the merged frame."""
        new = normalize_candles(df)
        existing = self.read(symbol, timeframe)
        merged = new if existing is None or existing.empty \
            else normalize_candles(pd.concat([existing, new]))
        path = self.path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(path)
        logger.debug("cache write %s %s: %d rows -> %s",
                     symbol, _tf_key(timeframe), len(merged), path)
        return merged

    def last_ts(self, symbol: str, timeframe: Timeframe | str) -> pd.Timestamp | None:
        df = self.read(symbol, timeframe)
        if df is None or df.empty:
            return None
        return df.index[-1]


class CachedFeed(DataFeed):
    """Cache-first wrapper around another :class:`DataFeed`.

    * hit within the cached range -> no network
    * requested range extends past the cache -> fetch only the missing
      head and/or tail via the inner feed and merge back
    * ``inner=None`` -> cache-only mode; raises :class:`DataError` when a
      symbol has nothing cached.
    """

    def __init__(self, inner: DataFeed | None,
                 cache: CandleCache | None = None) -> None:
        self.inner = inner
        self.cache = cache or CandleCache()

    # ------------------------------------------------------------- candles
    def get_candles(self, symbol: str, timeframe: Timeframe,
                    start: dt.date, end: dt.date) -> pd.DataFrame:
        cached = self.cache.read(symbol, timeframe)

        if self.inner is None:
            if cached is None or cached.empty:
                raise DataError(f"nothing cached for {symbol} {_tf_key(timeframe)} "
                                "and no inner feed (cache-only mode)")
            return _slice(cached, start, end)

        for fetch_start, fetch_end in self._missing_ranges(cached, start, end):
            try:
                fresh = self.inner.get_candles(symbol, timeframe, fetch_start, fetch_end)
            except DataError:
                if cached is None or cached.empty:
                    raise
                logger.warning("delta fetch failed for %s %s %s..%s; serving cache",
                               symbol, _tf_key(timeframe), fetch_start, fetch_end)
                continue
            cached = self.cache.write(symbol, timeframe, fresh)

        if cached is None or cached.empty:
            raise DataError(f"no candles available for {symbol} {_tf_key(timeframe)}")
        return _slice(cached, start, end)

    @staticmethod
    def _missing_ranges(cached: pd.DataFrame | None, start: dt.date,
                        end: dt.date) -> list[tuple[dt.date, dt.date]]:
        if cached is None or cached.empty:
            return [(start, end)]
        first_day = cached.index[0].date()
        last_day = cached.index[-1].date()
        ranges: list[tuple[dt.date, dt.date]] = []
        if start < first_day:                       # missing head
            ranges.append((start, first_day))
        if end >= last_day:                         # missing/partial tail
            ranges.append((last_day, end))
        return ranges

    # -------------------------------------------------------------- quotes
    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        if self.inner is None:
            raise DataError("quotes unavailable in cache-only mode")
        return self.inner.get_quotes(symbols)


def _slice(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    lo = pd.Timestamp(start, tz=TZ)
    hi = pd.Timestamp(end, tz=TZ) + pd.Timedelta(days=1)
    return df[(df.index >= lo) & (df.index < hi)]
