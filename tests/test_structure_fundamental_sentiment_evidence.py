from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import structure_fundamental_sentiment_evidence as evidence


def test_classifies_successful_deep_research_into_fundamental_and_sentiment_scores() -> None:
    text = """
    Revenue growth and stable EBITDA improve earnings visibility. Core infrastructure capex is supportive.
    Legal probe closure and a stock rally are positive catalysts, but debt, governance scrutiny and quarterly loss remain risks.
    """

    snapshot = evidence.classify_deep_research_answer(
        symbol="NSE:ADANIENT-EQ",
        status="success",
        answer=text,
        citations=[{"url": "https://example.com/source"}],
        source_run_id=6,
    )

    assert snapshot.symbol == "NSE:ADANIENT-EQ"
    assert snapshot.source_run_id == 6
    assert snapshot.fundamental_label in {"acceptable", "strong", "mixed"}
    assert Decimal("0") <= snapshot.fundamental_score <= Decimal("25")
    assert snapshot.sentiment_label in {"positive", "mixed"}
    assert Decimal("0") <= snapshot.sentiment_score <= Decimal("20")
    assert snapshot.confidence == "medium"
    assert "research-derived" in snapshot.summary.lower()


def test_classifies_error_or_empty_research_as_insufficient_data() -> None:
    snapshot = evidence.classify_deep_research_answer(
        symbol="NSE:TEST-EQ",
        status="error",
        answer="",
        citations=[],
        source_run_id=99,
    )

    assert snapshot.fundamental_label == "insufficient_data"
    assert snapshot.sentiment_label == "insufficient_data"
    assert snapshot.fundamental_score == Decimal("0")
    assert snapshot.sentiment_score == Decimal("0")
    assert snapshot.confidence == "low"


def test_build_upsert_payload_preserves_sources_and_raw_counts() -> None:
    snapshot = evidence.classify_deep_research_answer(
        symbol="NSE:ABC-EQ",
        status="success",
        answer="growth profit margin positive catalyst risk debt",
        citations=[{"url": "https://example.com/a"}],
        source_run_id=7,
    )

    payload = evidence.build_upsert_payload(snapshot)

    assert payload["symbol"] == "NSE:ABC-EQ"
    assert payload["evidence_source"] == "deep_research"
    assert payload["source_run_id"] == 7
    assert payload["citations"] == [{"url": "https://example.com/a"}]
    assert payload["raw"]["positive_fundamental_hits"] >= 1
    assert payload["raw"]["negative_fundamental_hits"] >= 1
