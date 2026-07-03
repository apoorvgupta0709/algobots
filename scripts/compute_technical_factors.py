#!/usr/bin/env python3
"""Compute technical factors from stored market candles.

Stores one latest factor snapshot per symbol/resolution in research.factor_snapshots.
This is research/paper-trading infrastructure only; it does not place orders.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Sequence

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT_ROOT / "migrations" / "001_trading_research_schemas.sql"
DEFAULT_DATABASE_URL = "postgresql://" + "hermes" + "@" + "127.0.0.1" + ":" + "55432" + "/" + "finance_tracker"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
SOURCE = "technical_factor_engine"
FOUR_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class Candle:
    symbol: str
    resolution: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None


@dataclass(frozen=True)
class FactorSnapshot:
    symbol: str
    resolution: str
    ts: datetime
    factors: dict[str, str]
    raw: dict[str, object]


def q4(value: Decimal) -> Decimal:
    return value.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    rounded = q4(value)
    if rounded == rounded.to_integral():
        return str(rounded.to_integral())
    return format(rounded.normalize(), "f")


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    return q4(sum(values[-period:]) / Decimal(period))


def rsi(closes: Sequence[Decimal], period: int = 14) -> Decimal | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return None

    gains: list[Decimal] = []
    losses: list[Decimal] = []
    window = closes[-(period + 1) :]
    for previous, current in zip(window, window[1:]):
        change = current - previous
        if change >= 0:
            gains.append(change)
            losses.append(Decimal("0"))
        else:
            gains.append(Decimal("0"))
            losses.append(-change)

    average_gain = sum(gains) / Decimal(period)
    average_loss = sum(losses) / Decimal(period)
    if average_loss == 0:
        return Decimal("100") if average_gain > 0 else Decimal("50")
    relative_strength = average_gain / average_loss
    return q4(Decimal("100") - (Decimal("100") / (Decimal("1") + relative_strength)))


def true_range(candle: Candle, previous_close: Decimal | None) -> Decimal:
    high_low = candle.high - candle.low
    if previous_close is None:
        return high_low
    return max(high_low, abs(candle.high - previous_close), abs(candle.low - previous_close))


def atr(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(candles) < period + 1:
        return None

    ranges: list[Decimal] = []
    start = len(candles) - period
    for idx in range(start, len(candles)):
        ranges.append(true_range(candles[idx], candles[idx - 1].close))
    return q4(sum(ranges) / Decimal(period))


def ema_series(values: Sequence[Decimal], period: int) -> list[Decimal] | None:
    """Return the EMA series, seeded with the SMA of the first ``period`` values.

    ``series[i]`` aligns with ``values[period - 1 + i]``.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    multiplier = Decimal(2) / Decimal(period + 1)
    seed = sum(values[:period]) / Decimal(period)
    series = [seed]
    for value in values[period:]:
        previous = series[-1]
        series.append((value - previous) * multiplier + previous)
    return series


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    series = ema_series(values, period)
    if series is None:
        return None
    return q4(series[-1])


@dataclass(frozen=True)
class Macd:
    macd: Decimal | None
    signal: Decimal | None
    histogram: Decimal | None


def macd(values: Sequence[Decimal], fast: int = 12, slow: int = 26, signal: int = 9) -> Macd:
    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("periods must be positive")
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    fast_series = ema_series(values, fast)
    slow_series = ema_series(values, slow)
    if fast_series is None or slow_series is None:
        return Macd(macd=None, signal=None, histogram=None)
    # Align the fast EMA to the slow EMA start (slow begins later in the series).
    fast_aligned = fast_series[slow - fast :]
    macd_line = [f - s for f, s in zip(fast_aligned, slow_series)]
    macd_value = q4(macd_line[-1])
    signal_series = ema_series(macd_line, signal)
    if signal_series is None:
        return Macd(macd=macd_value, signal=None, histogram=None)
    signal_value = q4(signal_series[-1])
    histogram = q4(macd_line[-1] - signal_series[-1])
    return Macd(macd=macd_value, signal=signal_value, histogram=histogram)


def roc(closes: Sequence[Decimal], period: int) -> Decimal | None:
    """Rate of change over ``period`` candles, expressed as a fraction."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return None
    reference = closes[-(period + 1)]
    if reference == 0:
        return None
    return q4((closes[-1] - reference) / reference)


def donchian_high(highs: Sequence[Decimal], period: int) -> Decimal | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(highs) < period:
        return None
    return q4(max(highs[-period:]))


def donchian_low(lows: Sequence[Decimal], period: int) -> Decimal | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(lows) < period:
        return None
    return q4(min(lows[-period:]))


def classify_breakout(close: Decimal, highs: Sequence[Decimal], period: int) -> str:
    """Label an upside breakout against the prior ``period`` candles.

    Compares the latest close to the highest high of the ``period`` candles
    *before* the latest one, so a fresh close above that band reads as a breakout
    rather than trivially touching its own range. Downside moves are left to the
    bearish trend flag. Returns ``unknown`` when history is too short.
    """
    if len(highs) < period + 1:
        return "unknown"
    prior_high = max(highs[-(period + 1) : -1])
    return "yes" if close > prior_high else "no"


def relative_volume(volumes: Sequence[int | None], period: int = 20) -> Decimal | None:
    if period <= 0:
        raise ValueError("period must be positive")
    if len(volumes) < period + 1:
        return None
    latest = volumes[-1]
    previous = volumes[-(period + 1) : -1]
    if latest is None or any(value is None for value in previous):
        return None
    average_previous = Decimal(sum(int(value) for value in previous)) / Decimal(period)
    if average_previous == 0:
        return None
    return q4(Decimal(int(latest)) / average_previous)


def classify_trend(close: Decimal, sma_20: Decimal | None, sma_50: Decimal | None) -> str:
    if sma_20 is None or sma_50 is None:
        return "unknown"
    if close > sma_20 > sma_50:
        return "bullish"
    if close < sma_20 < sma_50:
        return "bearish"
    return "neutral"


def classify_volatility(atr_pct: Decimal | None) -> str:
    if atr_pct is None:
        return "unknown"
    if atr_pct >= Decimal("0.03"):
        return "high"
    if atr_pct <= Decimal("0.01"):
        return "low"
    return "normal"


def compute_latest_snapshot(candles: Sequence[Candle]) -> FactorSnapshot:
    if len(candles) < 50:
        raise ValueError("technical factor engine requires at least 50 candles")
    ordered = sorted(candles, key=lambda candle: candle.ts)
    latest = ordered[-1]
    closes = [candle.close for candle in ordered]
    highs = [candle.high for candle in ordered]
    lows = [candle.low for candle in ordered]
    volumes = [candle.volume for candle in ordered]

    sma_20 = sma(closes, 20)
    sma_50 = sma(closes, 50)
    sma_200 = sma(closes, 200)
    ema_20 = ema(closes, 20)
    rsi_14 = rsi(closes, 14)
    atr_14 = atr(ordered, 14)
    atr_pct_14 = q4(atr_14 / latest.close) if atr_14 is not None and latest.close != 0 else None
    rel_vol_20 = relative_volume(volumes, 20)
    macd_result = macd(closes, 12, 26, 9)
    roc_20 = roc(closes, 20)
    roc_60 = roc(closes, 60)
    donchian_20_high = donchian_high(highs, 20)
    donchian_20_low = donchian_low(lows, 20)
    donchian_55_high = donchian_high(highs, 55)
    donchian_55_low = donchian_low(lows, 55)
    breakout_20 = classify_breakout(latest.close, highs, 20)
    breakout_55 = classify_breakout(latest.close, highs, 55)
    trend = classify_trend(latest.close, sma_20, sma_50)
    volatility_regime = classify_volatility(atr_pct_14)

    previous = ordered[-2] if len(ordered) >= 2 else None
    previous_close = previous.close if previous is not None else None
    gap_pct = (
        q4((latest.open - previous_close) / previous_close)
        if previous_close is not None and previous_close != 0
        else None
    )

    factors: dict[str, str] = {
        "close": decimal_text(latest.close),
        "trend": trend,
        "volatility_regime": volatility_regime,
        "breakout_20": breakout_20,
        "breakout_55": breakout_55,
    }
    optional_values = {
        "sma_20": sma_20,
        "sma_50": sma_50,
        "sma_200": sma_200,
        "ema_20": ema_20,
        "rsi_14": rsi_14,
        "atr_14": atr_14,
        "atr_pct_14": atr_pct_14,
        "relative_volume_20": rel_vol_20,
        "macd_12_26": macd_result.macd,
        "macd_signal_9": macd_result.signal,
        "macd_histogram": macd_result.histogram,
        "roc_20": roc_20,
        "roc_60": roc_60,
        "donchian_20_high": donchian_20_high,
        "donchian_20_low": donchian_20_low,
        "donchian_55_high": donchian_55_high,
        "donchian_55_low": donchian_55_low,
        "previous_day_high": previous.high if previous is not None else None,
        "previous_day_low": previous.low if previous is not None else None,
        "previous_day_close": previous_close,
        "gap_pct": gap_pct,
    }
    for key, value in optional_values.items():
        if value is not None:
            factors[key] = decimal_text(value)

    raw = {
        "engine": SOURCE,
        "input_candles": len(ordered),
        "minimum_required_candles": 50,
        "windows": {
            "sma_20": 20,
            "sma_50": 50,
            "sma_200": 200,
            "ema_20": 20,
            "rsi_14": 14,
            "atr_14": 14,
            "relative_volume_20": 20,
            "macd": {"fast": 12, "slow": 26, "signal": 9},
            "roc_20": 20,
            "roc_60": 60,
            "donchian_20": 20,
            "donchian_55": 55,
        },
    }
    return FactorSnapshot(symbol=latest.symbol, resolution=latest.resolution, ts=latest.ts, factors=factors, raw=raw)


def connect_db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def apply_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text())
    conn.commit()


def fetch_candles(conn: psycopg.Connection, symbol: str, resolution: str, limit: int = 120) -> list[Candle]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select symbol, resolution, ts, open, high, low, close, volume
            from market.candles
            where symbol = %s and resolution = %s
            order by ts desc
            limit %s
            """,
            (symbol, resolution, limit),
        )
        rows = cur.fetchall()
    return [
        Candle(
            symbol=row[0],
            resolution=row[1],
            ts=row[2],
            open=Decimal(row[3]),
            high=Decimal(row[4]),
            low=Decimal(row[5]),
            close=Decimal(row[6]),
            volume=row[7],
        )
        for row in reversed(rows)
    ]


def discover_symbol_resolutions(conn: psycopg.Connection, resolution: str | None = None) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        if resolution:
            cur.execute(
                """
                select symbol, resolution
                from market.candles
                where resolution = %s
                group by symbol, resolution
                having count(*) >= 50
                order by symbol, resolution
                """,
                (resolution,),
            )
        else:
            cur.execute(
                """
                select symbol, resolution
                from market.candles
                group by symbol, resolution
                having count(*) >= 50
                order by symbol, resolution
                """
            )
        return [(row[0], row[1]) for row in cur.fetchall()]


def store_factor_snapshots(conn: psycopg.Connection, snapshots: Iterable[FactorSnapshot]) -> int:
    rows = 0
    with conn.cursor() as cur:
        for snapshot in snapshots:
            cur.execute(
                """
                insert into research.factor_snapshots(symbol, resolution, ts, factors, source, raw)
                values (%s, %s, %s, %s::jsonb, %s, %s::jsonb)
                on conflict(symbol, resolution, ts, source) do update set
                    factors = excluded.factors,
                    raw = excluded.raw,
                    created_at = now()
                """,
                (
                    snapshot.symbol,
                    snapshot.resolution,
                    snapshot.ts,
                    json.dumps(snapshot.factors),
                    SOURCE,
                    json.dumps(snapshot.raw),
                ),
            )
            rows += 1
    conn.commit()
    return rows


def compute_for_symbols(conn: psycopg.Connection, symbols: Sequence[str], resolution: str, lookback: int) -> list[FactorSnapshot]:
    snapshots: list[FactorSnapshot] = []
    for symbol in symbols:
        candles = fetch_candles(conn, symbol, resolution, limit=lookback)
        snapshots.append(compute_latest_snapshot(candles))
    return snapshots


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute latest technical factor snapshots from market.candles")
    parser.add_argument("--symbols", nargs="+", help="Symbols to compute, e.g. NSE:TVSMOTOR-EQ. Defaults to all symbols with enough candles.")
    parser.add_argument("--resolution", default="D", help="Candle resolution to compute; default D")
    parser.add_argument("--lookback", type=int, default=120, help="Candles to load per symbol; default 120")
    parser.add_argument("--skip-schema", action="store_true", help="Do not apply trading/research migration first")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with connect_db() as conn:
        if not args.skip_schema:
            apply_schema(conn)
        if args.symbols:
            symbol_resolutions = [(symbol, args.resolution) for symbol in args.symbols]
        else:
            symbol_resolutions = discover_symbol_resolutions(conn, args.resolution)
        if not symbol_resolutions:
            print("No symbols with at least 50 candles found")
            return
        snapshots = [
            compute_latest_snapshot(fetch_candles(conn, symbol, resolution, args.lookback))
            for symbol, resolution in symbol_resolutions
        ]
        rows = store_factor_snapshots(conn, snapshots)
    for snapshot in snapshots:
        print(
            f"{snapshot.symbol} {snapshot.resolution} {snapshot.ts.isoformat()} "
            f"close={snapshot.factors.get('close')} trend={snapshot.factors.get('trend')} "
            f"rsi_14={snapshot.factors.get('rsi_14')} atr_pct_14={snapshot.factors.get('atr_pct_14')}"
        )
    print(f"Stored {rows} technical factor snapshots")


if __name__ == "__main__":
    main()
