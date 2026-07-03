from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import generate_daily_market_report as report

DATABASE_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"

def sample_row(**overrides):
    base = report.ReportRow(
        symbol="NSE:TVSMOTOR-EQ",
        resolution="D",
        factor_ts=datetime(2026, 6, 2, tzinfo=timezone.utc),
        quote_updated_at=datetime(2026, 6, 2, 5, 20, tzinfo=timezone.utc),
        quote_time=datetime(2026, 6, 2, tzinfo=timezone.utc),
        ltp=Decimal("3306.40"),
        previous_close=Decimal("3344.40"),
        candle_close=Decimal("3371.80"),
        trend="bearish",
        sma_20=Decimal("3464.955"),
        sma_50=Decimal("3527.008"),
        sma_200=Decimal("3290.120"),
        ema_20=Decimal("3450.220"),
        rsi_14=Decimal("34.9411"),
        atr_pct_14=Decimal("0.0327"),
        relative_volume_20=Decimal("0.2694"),
        volatility_regime="high",
        macd_12_26=Decimal("-12.5000"),
        macd_signal_9=Decimal("-8.2000"),
        macd_histogram=Decimal("-4.3000"),
        roc_20=Decimal("-0.0250"),
        roc_60=Decimal("0.0410"),
        donchian_20_high=Decimal("3590.000"),
        donchian_20_low=Decimal("3280.000"),
        donchian_55_high=Decimal("3700.000"),
        donchian_55_low=Decimal("3150.000"),
        previous_day_high=Decimal("3390.000"),
        previous_day_low=Decimal("3320.000"),
        previous_day_close=Decimal("3344.400"),
        gap_pct=Decimal("-0.0250"),
        breakout_20="no",
        breakout_55="no",
    )
    return replace(base, **overrides)


def test_format_inr_and_pct_are_telegram_friendly() -> None:
    assert report.format_inr(Decimal("3306.4")) == "₹3,306.40"
    assert report.format_pct(Decimal("-0.01136")) == "-1.14%"
    assert report.format_decimal(None) == "n/a"


def test_validate_args_rejects_invalid_report_inputs() -> None:
    with pytest.raises(SystemExit, match="--limit must be positive"):
        report.validate_args(argparse.Namespace(limit=0, resolution=None))
    with pytest.raises(SystemExit, match="--resolution must be non-empty"):
        report.validate_args(argparse.Namespace(limit=1, resolution="  "))


def test_classify_flags_identifies_risk_and_setups_without_trade_instruction() -> None:
    flags = report.classify_flags(sample_row())

    assert "Bearish daily candle trend: close below SMA20/SMA50" in flags
    assert "High volatility: ATR% 3.27%" in flags
    assert "Low participation: relative volume 0.2694" in flags
    assert all("buy" not in flag.lower() and "sell" not in flag.lower() for flag in flags)


def test_classify_flags_surfaces_breakout_gap_and_macd_momentum() -> None:
    flags = report.classify_flags(
        sample_row(
            breakout_20="yes",
            breakout_55="no",
            macd_histogram=Decimal("4.3000"),
            gap_pct=Decimal("0.0310"),
        )
    )

    assert any("Breakout watch" in flag and "20-day" in flag for flag in flags)
    assert "MACD positive momentum: histogram 4.3" in flags
    assert "Gap risk: opening gap 3.10%" in flags
    assert all("buy" not in flag.lower() and "sell" not in flag.lower() for flag in flags)


def test_classify_flags_reports_negative_macd_momentum() -> None:
    flags = report.classify_flags(sample_row())

    assert "MACD negative momentum: histogram -4.3" in flags


def test_render_report_shows_new_technical_facts() -> None:
    text = report.render_report([sample_row()], generated_at=datetime(2026, 6, 2, tzinfo=timezone.utc))

    assert "MACD(12,26):" in text
    assert "histogram -4.3" in text
    assert "ROC20 / ROC60:" in text
    assert "Donchian20 range:" in text
    assert "Gap:" in text
    assert "Breakout20 / Breakout55: no / no" in text
    assert "SMA200" in text
    assert "Data freshness:" in text


def test_classify_flags_surfaces_stale_factor_and_quote_data() -> None:
    flags = report.classify_flags(
        sample_row(
            factor_ts=datetime(2026, 5, 20, tzinfo=timezone.utc),
            quote_updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            quote_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ),
        generated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )

    assert "Data freshness: factor snapshot is 14d old" in flags
    assert "Data freshness: quote snapshot is 2d old" in flags


def test_classify_flags_surfaces_missing_quote_data() -> None:
    flags = report.classify_flags(
        sample_row(quote_updated_at=None, quote_time=None),
        generated_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
    )

    assert "Data freshness: latest quote is missing" in flags


def test_quote_freshness_prefers_market_quote_time_over_db_write_time() -> None:
    flags = report.classify_flags(
        sample_row(
            quote_time=datetime(2026, 5, 30, tzinfo=timezone.utc),
            quote_updated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
        ),
        generated_at=datetime(2026, 6, 3, 12, tzinfo=timezone.utc),
    )

    assert "Data freshness: quote snapshot is 4d old" in flags


def test_render_report_labels_non_daily_resolution_correctly() -> None:
    expected = {
        "W": "Weekly candle",
        "M": "Monthly candle",
        "60": "60-minute candle",
    }
    for resolution, label in expected.items():
        text = report.render_report(
            [sample_row(resolution=resolution)],
            generated_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
        )

        assert f"{label} close:" in text
        assert f"{label} trend: bearish" in text
        assert f"Bearish {label.lower()} trend: close below SMA20/SMA50" in text
        assert "Daily candle trend" not in text


def test_render_report_separates_facts_from_suggestions_and_no_order_language() -> None:
    rows = [sample_row()]

    text = report.render_report(rows, generated_at=datetime(2026, 6, 2, 10, 55, tzinfo=timezone.utc))

    assert "## Daily Market Report" in text
    assert "## Facts" in text
    assert "## Risk flags / setups" in text
    assert "## Suggested next actions" in text
    assert "NSE:TVSMOTOR-EQ" in text
    assert "LTP: ₹3,306.40" in text
    assert "Change: ₹-38.00 (-1.14%)" in text
    assert "Daily candle trend: bearish" in text
    assert "Not trade advice" in text
    forbidden = ["place order", "execute trade", "buy now", "sell now"]
    assert all(term not in text.lower() for term in forbidden)


def test_render_report_handles_no_rows() -> None:
    text = report.render_report([], generated_at=datetime(2026, 6, 2, 10, 55, tzinfo=timezone.utc))

    assert "No factor snapshots found" in text


def test_fetch_report_rows_with_symbol_filter_uses_valid_query() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute((PROJECT_ROOT / "migrations" / "001_trading_research_schemas.sql").read_text())
            cur.execute(
                """
                delete from research.factor_snapshots
                where symbol = 'NSE:REPORTTEST-EQ' and source = 'technical_factor_engine'
                """
            )
            cur.execute(
                """
                insert into market.instruments(symbol, exchange)
                values ('NSE:REPORTTEST-EQ', 'NSE')
                on conflict(symbol) do nothing
                """
            )
            cur.execute(
                """
                insert into market.quotes(symbol, ltp, close, quote_time)
                values ('NSE:REPORTTEST-EQ', 100, 95, '2026-06-02T00:00:00Z')
                on conflict(symbol) do update set ltp = excluded.ltp, close = excluded.close, quote_time = excluded.quote_time
                """
            )
            cur.execute(
                """
                insert into research.factor_snapshots(symbol, resolution, ts, factors, source)
                values (
                    'NSE:REPORTTEST-EQ', 'D', '2026-06-02T00:00:00Z',
                    '{"close":"100","trend":"bullish","sma_20":"90","sma_50":"80"}'::jsonb,
                    'technical_factor_engine'
                )
                on conflict(symbol, resolution, ts, source) do update set factors = excluded.factors
                """
            )
            conn.commit()

        rows = report.fetch_report_rows(conn, ["NSE:REPORTTEST-EQ"], 5, "D")

        assert len(rows) == 1
        assert rows[0].symbol == "NSE:REPORTTEST-EQ"
        assert rows[0].resolution == "D"
        assert rows[0].ltp == Decimal("100.000000")

        with conn.cursor() as cur:
            cur.execute("delete from research.factor_snapshots where symbol = 'NSE:REPORTTEST-EQ'")
            cur.execute("delete from market.quotes where symbol = 'NSE:REPORTTEST-EQ'")
            cur.execute("delete from market.instruments where symbol = 'NSE:REPORTTEST-EQ'")
        conn.commit()


def test_fetch_report_rows_filters_requested_resolution() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute((PROJECT_ROOT / "migrations" / "001_trading_research_schemas.sql").read_text())
            cur.execute("delete from research.factor_snapshots where symbol = 'NSE:RESFILTER-EQ'")
            cur.execute(
                """
                insert into market.instruments(symbol, exchange)
                values ('NSE:RESFILTER-EQ', 'NSE')
                on conflict(symbol) do nothing
                """
            )
            cur.execute(
                """
                insert into research.factor_snapshots(symbol, resolution, ts, factors, source)
                values
                    ('NSE:RESFILTER-EQ', 'D', '2026-06-02T00:00:00Z', '{"close":"100","trend":"bullish"}'::jsonb, 'technical_factor_engine'),
                    ('NSE:RESFILTER-EQ', 'W', '2026-06-03T00:00:00Z', '{"close":"110","trend":"bearish"}'::jsonb, 'technical_factor_engine')
                on conflict(symbol, resolution, ts, source) do update set factors = excluded.factors
                """
            )
            conn.commit()

        rows = report.fetch_report_rows(conn, ["NSE:RESFILTER-EQ"], 5, "D")

        assert len(rows) == 1
        assert rows[0].symbol == "NSE:RESFILTER-EQ"
        assert rows[0].resolution == "D"
        assert rows[0].candle_close == Decimal("100")

        with conn.cursor() as cur:
            cur.execute("delete from research.factor_snapshots where symbol = 'NSE:RESFILTER-EQ'")
            cur.execute("delete from market.instruments where symbol = 'NSE:RESFILTER-EQ'")
        conn.commit()
