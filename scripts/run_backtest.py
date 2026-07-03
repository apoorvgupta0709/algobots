#!/usr/bin/env python
"""CLI backtest runner.

Usage:
    python scripts/run_backtest.py --strategy id01_orb --days 200
    python scripts/run_backtest.py --strategy lt02_sip --days 500 --capital 300000 --no-persist

Per resolved instrument the runner tries real cached candles (via the data
subsystem, when available) and otherwise generates deterministic synthetic
fixture data. The run is printed as a metrics table and persisted to the
backtest_runs / backtest_trades tables unless --no-persist is given.
"""
from __future__ import annotations

import argparse
import logging
import sys
import zlib
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)

from algobot.backtest.compat import ensure_strategy_deps  # noqa: E402

ensure_strategy_deps()  # shim missing sibling modules BEFORE strategy discovery

from algobot.backtest.engine import BacktestEngine  # noqa: E402
from algobot.core import universes  # noqa: E402
from algobot.core.enums import Timeframe  # noqa: E402
from algobot.core.registry import get_strategy  # noqa: E402
from tests.fixtures.synthetic import equity_daily, index_5min  # noqa: E402

log = logging.getLogger("run_backtest")

_OHLCV_AGG = {"open": "first", "high": "max", "low": "min",
              "close": "last", "volume": "sum"}


def _try_real(symbol: str, timeframe: Timeframe, days: int) -> pd.DataFrame | None:
    """Real cached candles from the data subsystem, if present and fresh enough."""
    try:
        from algobot.data.cache import CandleCache  # lazy
        df = CandleCache().read(symbol, timeframe)
        if df is None or df.empty or "close" not in df.columns:
            return None
        cutoff = df.index[-1] - pd.Timedelta(days=days * 1.6)
        df = df[df.index >= cutoff]
        return df if len(df) else None
    except Exception:
        return None


def _synthetic(symbol: str, timeframe: Timeframe, days: int, base_seed: int) -> pd.DataFrame:
    seed = base_seed + zlib.crc32(symbol.encode()) % 10_000
    start_price = 24_000.0 if "INDEX" in symbol.upper() else 800.0
    if timeframe in (Timeframe.MIN5, Timeframe.MIN15, Timeframe.HOUR1):
        df = index_5min(days=days, seed=seed, start_price=start_price)
        if timeframe == Timeframe.MIN15:
            df = df.resample("15min").agg(_OHLCV_AGG).dropna()
        elif timeframe == Timeframe.HOUR1:
            df = df.resample("60min", offset="15min").agg(_OHLCV_AGG).dropna()
        return df
    return equity_daily(days=days, seed=seed, start_price=start_price)


def load_data(meta, days: int, base_seed: int = 100) -> tuple[dict[str, pd.DataFrame], str]:
    """Frames for every resolved instrument + a candle data_source label."""
    symbols = universes.resolve(meta.instruments)
    frames: dict[str, pd.DataFrame] = {}
    sources: set[str] = set()
    for sym in symbols:
        df = _try_real(sym, meta.timeframe, days)
        if df is not None and len(df) > meta.warmup_bars:
            sources.add("real")
        else:
            df = _synthetic(sym, meta.timeframe, days, base_seed)
            sources.add("synthetic")
        frames[sym] = df
    label = "mixed" if len(sources) > 1 else (next(iter(sources)) if sources else "synthetic")
    return frames, label


def combine_sources(*labels: str) -> str:
    kinds = {k for label in labels for k in
             (("real", "synthetic") if label == "mixed" else (label,))}
    return "mixed" if len(kinds) > 1 else next(iter(kinds))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a strategy backtest")
    parser.add_argument("--strategy", required=True, help="strategy_id from the registry")
    parser.add_argument("--days", type=int, default=200, help="lookback trading days")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--no-persist", action="store_true",
                        help="skip writing backtest_runs/backtest_trades rows")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    cls = get_strategy(args.strategy)
    strategy = cls()
    data, candle_source = load_data(cls.meta, args.days)
    log.info("Loaded %d instrument(s) [%s candles] for %s",
             len(data), candle_source, args.strategy)

    engine = BacktestEngine(strategy, data, capital=args.capital)
    result = engine.run()
    # honest data_source: candles AND option pricing provenance combined
    result.data_source = combine_sources(candle_source, result.data_source)

    print(f"\n=== {cls.meta.name} ({args.strategy}) "
          f"{result.start} .. {result.end}  [{result.data_source} data] ===")
    for key, value in result.metrics.items():
        print(f"  {key:>18}: {value:>14,.4f}" if isinstance(value, float)
              else f"  {key:>18}: {value:>14}")
    print(f"  {'open_positions':>18}: {len(result.open_positions):>14}")

    if not args.no_persist:
        run_id = result.persist()
        print(f"  {'run_id':>18}: {run_id:>14}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
