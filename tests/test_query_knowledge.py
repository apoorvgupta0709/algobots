from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import query_knowledge as qk


def make_hit(**overrides) -> qk.SearchHit:
    base = dict(
        chunk_id=10,
        source_id=1,
        title="Trading Book",
        author="A. Author",
        chapter="Chapter 3 Stops",
        page_start=41,
        page_end=43,
        content="Use a stop.",
        score=0.05,
    )
    base.update(overrides)
    return qk.SearchHit(**base)


def test_rrf_merge_rewards_presence_in_both_lists() -> None:
    scores = qk.rrf_merge([[1, 2, 3], [2, 1]], k=60)

    assert set(scores) == {1, 2, 3}
    assert scores[1] > scores[3]
    assert scores[2] > scores[3]


def test_rrf_merge_orders_by_rank_within_one_list() -> None:
    scores = qk.rrf_merge([[5, 6]], k=60)
    assert scores[5] > scores[6]


def test_rrf_merge_empty_input() -> None:
    assert qk.rrf_merge([], k=60) == {}
    assert qk.rrf_merge([[], []], k=60) == {}


def test_format_citation_full() -> None:
    assert qk.format_citation(make_hit()) == "Trading Book — Chapter 3 Stops, pages 41–43"


def test_format_citation_single_page_and_missing_fields() -> None:
    assert (
        qk.format_citation(make_hit(page_start=41, page_end=41))
        == "Trading Book — Chapter 3 Stops, page 41"
    )
    assert (
        qk.format_citation(make_hit(chapter=None, page_start=None, page_end=None))
        == "Trading Book"
    )


def test_search_sql_is_select_only() -> None:
    statements = [
        qk.vector_search_sql(book_filter=False),
        qk.vector_search_sql(book_filter=True),
        qk.fts_search_sql(book_filter=False),
        qk.fts_search_sql(book_filter=True),
        qk.DETAIL_SQL,
    ]
    for sql in statements:
        lowered = sql.lower()
        assert lowered.strip().startswith("select")
        for keyword in ("insert ", "update ", "delete ", "drop ", "alter ", "truncate "):
            assert keyword not in lowered


def test_book_filter_only_added_when_requested() -> None:
    assert "ilike" not in qk.vector_search_sql(book_filter=False)
    assert "ilike" in qk.vector_search_sql(book_filter=True)
    assert "ilike" not in qk.fts_search_sql(book_filter=False)
    assert "ilike" in qk.fts_search_sql(book_filter=True)


def test_hit_json_shape() -> None:
    payload = dataclasses.asdict(make_hit())
    assert set(payload) == {
        "chunk_id",
        "source_id",
        "title",
        "author",
        "chapter",
        "page_start",
        "page_end",
        "content",
        "score",
    }


def test_format_citation_page_start_only() -> None:
    assert (
        qk.format_citation(make_hit(page_start=41, page_end=None))
        == "Trading Book — Chapter 3 Stops, page 41"
    )


def test_run_search_rejects_non_positive_top_k() -> None:
    assert qk.run_search(conn=None, query_text="x", query_vector=[0.0], top_k=0, book=None) == []
    assert qk.run_search(conn=None, query_text="x", query_vector=[0.0], top_k=-3, book=None) == []
