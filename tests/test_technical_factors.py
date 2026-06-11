from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import compute_technical_factors as factors

DATABASE_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"


def make_candles(count: int, symbol: str = "NSE:FACTORTEST-EQ") -> list[factors.Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        factors.Candle(
            symbol=symbol,
            resolution="D",
            ts=start + timedelta(days=i),
            open=Decimal(i + 1),
            high=Decimal(i + 2),
            low=Decimal(i),
            close=Decimal(i + 1),
            volume=1000 + (i * 10),
        )
        for i in range(count)
    ]


def test_indicator_helpers_compute_sma_rsi_atr_and_relative_volume() -> None:
    candles = make_candles(60)
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]

    assert factors.sma(closes, 3) == Decimal("59")
    assert factors.rsi(closes, 14) == Decimal("100")
    assert factors.atr(candles, 14) == Decimal("2")
    assert factors.relative_volume(volumes, 20) == Decimal("1.0707")


def test_latest_snapshot_contains_expected_factor_keys_and_bullish_trend() -> None:
    candles = make_candles(60)

    snapshot = factors.compute_latest_snapshot(candles)

    assert snapshot.symbol == "NSE:FACTORTEST-EQ"
    assert snapshot.resolution == "D"
    assert snapshot.ts == candles[-1].ts
    assert snapshot.factors["close"] == "60"
    assert snapshot.factors["sma_20"] == "50.5"
    assert snapshot.factors["sma_50"] == "35.5"
    assert snapshot.factors["rsi_14"] == "100"
    assert snapshot.factors["atr_14"] == "2"
    assert snapshot.factors["atr_pct_14"] == "0.0333"
    assert snapshot.factors["relative_volume_20"] == "1.0707"
    assert snapshot.factors["trend"] == "bullish"
    assert snapshot.factors["volatility_regime"] == "high"


def test_latest_snapshot_contains_extended_technical_factors() -> None:
    candles = make_candles(70)

    snapshot = factors.compute_latest_snapshot(candles)

    assert snapshot.factors["ema_20"] == "60.5"
    assert snapshot.factors["macd_12_26"] == "7"
    assert snapshot.factors["macd_signal_9"] == "7"
    assert snapshot.factors["macd_histogram"] == "0"
    assert snapshot.factors["roc_20"] == "0.4"
    assert snapshot.factors["roc_60"] == "6"
    assert snapshot.factors["donchian_20_high"] == "71"
    assert snapshot.factors["donchian_20_low"] == "50"
    assert snapshot.factors["donchian_55_high"] == "71"
    assert snapshot.factors["donchian_55_low"] == "15"
    assert snapshot.factors["previous_day_high"] == "70"
    assert snapshot.factors["previous_day_low"] == "68"
    assert snapshot.factors["previous_day_close"] == "69"
    assert snapshot.factors["gap_pct"] == "0.0145"
    assert snapshot.factors["breakout_20"] == "no"
    assert snapshot.factors["breakout_55"] == "no"


def test_long_window_fields_are_optional_until_enough_history_exists() -> None:
    short_snapshot = factors.compute_latest_snapshot(make_candles(70))
    long_snapshot = factors.compute_latest_snapshot(make_candles(220))

    assert "sma_200" not in short_snapshot.factors
    assert long_snapshot.factors["sma_200"] == "120.5"


def test_breakout_label_compares_close_to_prior_range() -> None:
    candles = make_candles(70)
    latest = candles[-1]
    candles[-1] = factors.Candle(
        symbol=latest.symbol,
        resolution=latest.resolution,
        ts=latest.ts,
        open=latest.open,
        high=Decimal("101"),
        low=latest.low,
        close=Decimal("100"),
        volume=latest.volume,
    )

    snapshot = factors.compute_latest_snapshot(candles)

    assert snapshot.factors["breakout_20"] == "yes"
    assert snapshot.factors["breakout_55"] == "yes"


def test_compute_latest_snapshot_requires_enough_candles() -> None:
    try:
        factors.compute_latest_snapshot(make_candles(49))
    except ValueError as exc:
        assert "at least 50 candles" in str(exc)
    else:
        raise AssertionError("expected ValueError for insufficient history")


def test_store_factor_snapshots_upserts_into_research_factor_snapshots() -> None:
    candles = make_candles(60, symbol="NSE:FACTORDBTEST-EQ")
    snapshot = factors.compute_latest_snapshot(candles)

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute((PROJECT_ROOT / "migrations" / "001_trading_research_schemas.sql").read_text())
            cur.execute(
                """
                delete from research.factor_snapshots
                where symbol = 'NSE:FACTORDBTEST-EQ' and resolution = 'D' and source = 'technical_factor_engine'
                """
            )
            conn.commit()

        inserted = factors.store_factor_snapshots(conn, [snapshot])
        assert inserted == 1
        inserted_again = factors.store_factor_snapshots(conn, [snapshot])
        assert inserted_again == 1

        with conn.cursor() as cur:
            cur.execute(
                """
                select factors->>'trend', factors->>'sma_20', count(*) over ()
                from research.factor_snapshots
                where symbol = 'NSE:FACTORDBTEST-EQ' and resolution = 'D' and source = 'technical_factor_engine'
                """
            )
            row = cur.fetchone()
            assert row == ("bullish", "50.5", 1)
            cur.execute(
                """
                delete from research.factor_snapshots
                where symbol = 'NSE:FACTORDBTEST-EQ' and resolution = 'D' and source = 'technical_factor_engine'
                """
            )
        conn.commit()
