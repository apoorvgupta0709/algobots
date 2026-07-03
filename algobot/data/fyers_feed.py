"""Live/historical data feed backed by the Fyers v3 REST API.

Wraps a ``fyers_apiv3.fyersModel.FyersModel`` instance (already authenticated;
see ``algobot.broker.fyers.auth``). Long history ranges are chunked to stay
inside Fyers' per-request limits, with a short pause between chunks to respect
the ~10 req/s rate limit.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd

from algobot.core.enums import Timeframe
from algobot.core.exceptions import DataError
from algobot.data.feed import TZ, CANDLE_COLUMNS, DataFeed, normalize_candles

logger = logging.getLogger(__name__)

# Fyers history API limits (days per request), conservative.
MAX_DAYS_INTRADAY = 90
MAX_DAYS_DAILY = 360
CHUNK_PAUSE_SEC = 0.25


class FyersFeed(DataFeed):
    """DataFeed over ``fyers.history`` / ``fyers.quotes``."""

    def __init__(self, fyers, chunk_pause: float = CHUNK_PAUSE_SEC) -> None:
        self.fyers = fyers
        self.chunk_pause = chunk_pause

    # ------------------------------------------------------------- candles
    def get_candles(self, symbol: str, timeframe: Timeframe,
                    start: dt.date, end: dt.date) -> pd.DataFrame:
        resolution = timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)
        if start > end:
            raise DataError(f"start {start} after end {end} for {symbol}")
        max_days = MAX_DAYS_DAILY if resolution.upper() == "D" else MAX_DAYS_INTRADAY

        candles: list[list] = []
        chunk_start = start
        first = True
        while chunk_start <= end:
            chunk_end = min(chunk_start + dt.timedelta(days=max_days - 1), end)
            if not first:
                time.sleep(self.chunk_pause)
            first = False
            candles.extend(self._history_chunk(symbol, resolution, chunk_start, chunk_end))
            chunk_start = chunk_end + dt.timedelta(days=1)

        if not candles:
            raise DataError(f"empty candles for {symbol} {resolution} {start}..{end}")
        frame = pd.DataFrame(candles, columns=["ts", *CANDLE_COLUMNS])
        frame["ts"] = pd.to_datetime(frame["ts"], unit="s", utc=True).dt.tz_convert(TZ)
        return normalize_candles(frame)

    def _history_chunk(self, symbol: str, resolution: str,
                       start: dt.date, end: dt.date) -> list[list]:
        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",                 # yyyy-mm-dd range bounds
            "range_from": start.isoformat(),
            "range_to": end.isoformat(),
            "cont_flag": "1",                   # continuous data for F&O
        }
        response = self.fyers.history(data=payload)
        if not isinstance(response, dict) or response.get("s") != "ok":
            raise DataError(f"fyers.history failed for {symbol} "
                            f"{start}..{end}: {response!r}")
        chunk = response.get("candles") or []
        logger.debug("history %s %s %s..%s -> %d candles",
                     symbol, resolution, start, end, len(chunk))
        return chunk

    # -------------------------------------------------------------- quotes
    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        response = self.fyers.quotes({"symbols": ",".join(symbols)})
        if not isinstance(response, dict) or response.get("s") != "ok":
            raise DataError(f"fyers.quotes failed for {symbols}: {response!r}")
        out: dict[str, float] = {}
        for item in response.get("d") or []:
            name = item.get("n")
            ltp = (item.get("v") or {}).get("lp")
            if name is not None and ltp is not None:
                out[name] = float(ltp)
        if not out:
            raise DataError(f"fyers.quotes returned no prices for {symbols}")
        return out
