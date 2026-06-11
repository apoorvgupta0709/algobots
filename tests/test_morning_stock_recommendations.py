from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import generate_daily_market_report as daily
from scripts import run_morning_stock_recommendations as morning


def sample_row(**overrides):
    base = daily.ReportRow(
        symbol="NSE:GOOD-EQ",
        resolution="D",
        factor_ts=datetime(2026, 6, 5, tzinfo=timezone.utc),
        quote_updated_at=datetime(2026, 6, 5, 5, 0, tzinfo=timezone.utc),
        quote_time=datetime(2026, 6, 5, 5, 0, tzinfo=timezone.utc),
        ltp=Decimal("210.00"),
        previous_close=Decimal("200.00"),
        candle_close=Decimal("208.00"),
        trend="bullish",
        sma_20=Decimal("190.00"),
        sma_50=Decimal("180.00"),
        sma_200=Decimal("170.00"),
        ema_20=Decimal("192.00"),
        rsi_14=Decimal("62.00"),
        atr_pct_14=Decimal("0.025"),
        relative_volume_20=Decimal("1.80"),
        volatility_regime="normal",
        macd_12_26=Decimal("5.0"),
        macd_signal_9=Decimal("3.0"),
        macd_histogram=Decimal("2.0"),
        roc_20=Decimal("0.08"),
        roc_60=Decimal("0.15"),
        donchian_20_high=Decimal("212.00"),
        donchian_20_low=Decimal("175.00"),
        donchian_55_high=Decimal("220.00"),
        donchian_55_low=Decimal("160.00"),
        previous_day_high=Decimal("205.00"),
        previous_day_low=Decimal("196.00"),
        previous_day_close=Decimal("200.00"),
        gap_pct=Decimal("0.010"),
        breakout_20="yes",
        breakout_55="no",
    )
    return replace(base, **overrides)


def test_score_buy_candidate_uses_technical_and_risk_evidence() -> None:
    candidate = morning.score_candidate(sample_row(), generated_at=datetime(2026, 6, 5, 8, tzinfo=timezone.utc))

    assert candidate.symbol == "NSE:GOOD-EQ"
    assert candidate.score >= 80
    assert candidate.label == "buy_candidate_research"
    assert candidate.entry_condition.startswith("Only consider")
    assert candidate.stop_loss is not None
    assert candidate.max_risk_note
    assert any("Bullish" in reason or "bullish" in reason for reason in candidate.reasons)


def test_score_rejects_stale_or_missing_data() -> None:
    candidate = morning.score_candidate(
        sample_row(
            quote_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
            quote_updated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ),
        generated_at=datetime(2026, 6, 5, 8, tzinfo=timezone.utc),
    )

    assert candidate.label == "needs_review"
    assert any("stale" in reason.lower() for reason in candidate.risks)


def test_candidate_with_stop_above_ltp_needs_review_not_buy_candidate() -> None:
    candidate = morning.score_candidate(
        sample_row(ltp=Decimal("190.00"), previous_day_low=Decimal("196.00")),
        generated_at=datetime(2026, 6, 5, 8, tzinfo=timezone.utc),
    )

    assert candidate.label == "needs_review"
    assert candidate.target is None
    assert any("stop" in risk.lower() for risk in candidate.risks)


def test_render_report_lists_buy_candidates_but_keeps_execution_disabled() -> None:
    candidates = [morning.score_candidate(sample_row(), generated_at=datetime(2026, 6, 5, 8, tzinfo=timezone.utc))]

    text = morning.render_recommendation_report(
        candidates,
        generated_at=datetime(2026, 6, 5, 8, tzinfo=timezone.utc),
        deep_research_notes={"NSE:GOOD-EQ": "Fundamental/sentiment: positive catalyst context, with source caveats."},
    )

    assert "Morning Stock Recommendations" in text
    assert "buy_candidate_research" in text
    assert "Fundamental/sentiment" in text
    assert "No orders placed" in text
    forbidden = ["buy now", "sell now", "execute trade", "place order"]
    assert all(term not in text.lower() for term in forbidden)


def test_migration_defines_signal_tables() -> None:
    sql = (PROJECT_ROOT / "migrations" / "003_morning_recommendations.sql").read_text()

    assert "research.signal_runs" in sql
    assert "research.signals" in sql
    assert "live_orders_enabled" in sql
