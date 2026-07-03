#!/usr/bin/env python
"""Fetch historical candles from Fyers into the parquet candle cache.

Usage:
    python scripts/fetch_history.py --symbols NSE:NIFTY50-INDEX,NSE:SBIN-EQ \
        --timeframe D --days 365

Requires Fyers credentials/token via ``algobot.broker.fyers.auth`` (imported
lazily — that module may still be under construction).
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from algobot.core.enums import Timeframe
from algobot.data.cache import CachedFeed, CandleCache
from algobot.data.fyers_feed import FyersFeed

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", required=True,
                        help="comma-separated Fyers symbols, e.g. NSE:NIFTY50-INDEX,NSE:SBIN-EQ")
    parser.add_argument("--timeframe", default="D",
                        choices=[tf.value for tf in Timeframe],
                        help="Fyers resolution (default: D)")
    parser.add_argument("--days", type=int, default=365,
                        help="lookback window in calendar days (default: 365)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    try:  # lazy: broker auth module is being built in parallel
        from algobot.broker.fyers.auth import get_fyers_client
    except ImportError as exc:
        print("ERROR: algobot.broker.fyers.auth is unavailable "
              f"({exc}). Build/install the broker auth module and ensure Fyers "
              "credentials + access token are configured before fetching history.",
              file=sys.stderr)
        return 1
    try:
        client = get_fyers_client()
    except Exception as exc:  # AuthError, missing .env, ...
        print(f"ERROR: could not create Fyers client: {exc}", file=sys.stderr)
        return 1

    feed = CachedFeed(FyersFeed(client), CandleCache())
    timeframe = Timeframe(args.timeframe)
    end = dt.date.today()
    start = end - dt.timedelta(days=args.days)

    failures = 0
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        try:
            df = feed.get_candles(symbol, timeframe, start, end)
            logger.info("%s %s: %d candles cached (%s .. %s)", symbol,
                        timeframe.value, len(df), df.index[0], df.index[-1])
        except Exception as exc:
            failures += 1
            logger.error("%s: fetch failed: %s", symbol, exc)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
