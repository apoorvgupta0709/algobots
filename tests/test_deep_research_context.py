from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import run_deep_research_context as deep


def test_normalize_symbols_accepts_plain_nse_codes() -> None:
    assert deep.normalize_symbols(["reliance", "NSE:TCS-EQ", "reliance"]) == [
        "NSE:RELIANCE-EQ",
        "NSE:TCS-EQ",
    ]


def test_build_stock_query_includes_local_context_and_no_order_instruction() -> None:
    query = deep.build_query(
        topic="Reliance Industries",
        symbols=["NSE:RELIANCE-EQ"],
        prompt_template="stock_context",
        local_context="LTP ₹1,000; trend bullish",
        lookback_days=90,
    )

    assert "Reliance Industries" in query
    assert "NSE:RELIANCE-EQ" in query
    assert "LTP ₹1,000" in query
    assert "invalidate a bullish trading thesis" in query
    assert "place order" not in query.lower()


def test_extract_citations_from_annotations_and_content_urls() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": "See https://example.com/story.",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {"url": "https://example.com/filing", "title": "Filing"},
                        }
                    ],
                }
            }
        ]
    }

    citations = deep.extract_citations(response)

    assert {item["url"] for item in citations} == {
        "https://example.com/filing",
        "https://example.com/story",
    }


def test_parse_openrouter_response_preserves_usage_and_cost() -> None:
    response = {
        "model": "perplexity/sonar-deep-research",
        "provider": "OpenRouter",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "cost": 0.1234},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": "# Answer\nSource-backed synthesis.",
                    "annotations": [],
                },
            }
        ],
    }

    result = deep.parse_openrouter_response(
        response,
        topic="Auto sector",
        symbols=["NSE:TVSMOTOR-EQ"],
        prompt_template="sector_context",
        query="Research auto sector",
        output_format="markdown_report",
    )

    assert result.status == "success"
    assert result.answer.startswith("# Answer")
    assert result.cost == Decimal("0.1234")
    assert result.usage["total_tokens"] == 30
    assert result.finish_reason == "stop"


def test_render_report_keeps_research_read_only() -> None:
    result = deep.DeepResearchResult(
        topic="Reliance",
        symbols=["NSE:RELIANCE-EQ"],
        prompt_template="stock_context",
        query="Research Reliance",
        answer="Bull case and bear case with caveats.",
        citations=[{"title": "Example", "url": "https://example.com"}],
        model="perplexity/sonar-deep-research",
        provider="openrouter",
        output_format="markdown_report",
        usage={"total_tokens": 42},
        cost=None,
        finish_reason="stop",
        status="success",
        error=None,
        raw={},
    )

    report = deep.render_deep_research_report(result, "Local FYERS facts")

    assert "Not trade advice" in report
    assert "no orders placed" in report
    assert "Local FYERS facts" in report
    assert "https://example.com" in report
    forbidden = ["buy now", "sell now", "place order", "execute trade"]
    assert all(term not in report.lower() for term in forbidden)


def test_migration_defines_deep_research_runs_table() -> None:
    sql = (PROJECT_ROOT / "migrations" / "002_deep_research_runs.sql").read_text()

    assert "research.deep_research_runs" in sql
    assert "citations jsonb" in sql
    assert "symbols text[]" in sql
    assert "no order placement" in sql.lower() or "read-only research" in sql.lower()
