#!/usr/bin/env python3
"""Hybrid semantic + full-text search over ingested trading books.

Strictly read-only: opens a read-only transaction, emits SELECT statements
only, makes no FYERS or LLM calls. Intended for the Hermes agent and humans:

    uv run python scripts/query_knowledge.py "trailing stops for long options" --top-k 8
    uv run python scripts/query_knowledge.py "position sizing" --book "Van Tharp" --json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ingest_books import (  # noqa: E402
    DEFAULT_DATABASE_URL,
    build_embedder,
    load_config,
    vector_literal,
)

RRF_K = 60
CANDIDATE_MULTIPLIER = 4  # fetch top_k * multiplier from each ranker before fusion


@dataclass(frozen=True)
class SearchHit:
    chunk_id: int
    source_id: int
    title: str
    author: str | None
    chapter: str | None
    page_start: int | None
    page_end: int | None
    content: str
    score: float


def vector_search_sql(book_filter: bool) -> str:
    sql = (
        "select c.chunk_id from knowledge.chunks c"
        " join knowledge.sources s on s.source_id = c.source_id"
        " where c.embedding is not null"
    )
    if book_filter:
        sql += " and s.title ilike %(book)s"
    sql += " order by c.embedding <=> %(query_vector)s::vector limit %(limit)s"
    return sql


def fts_search_sql(book_filter: bool) -> str:
    sql = (
        "select c.chunk_id from knowledge.chunks c"
        " join knowledge.sources s on s.source_id = c.source_id"
        " where c.tsv @@ websearch_to_tsquery('english', %(query_text)s)"
    )
    if book_filter:
        sql += " and s.title ilike %(book)s"
    sql += (
        " order by ts_rank(c.tsv, websearch_to_tsquery('english', %(query_text)s)) desc"
        " limit %(limit)s"
    )
    return sql


DETAIL_SQL = (
    "select c.chunk_id, c.source_id, s.title, s.author, c.chapter,"
    " c.page_start, c.page_end, c.content"
    " from knowledge.chunks c"
    " join knowledge.sources s on s.source_id = c.source_id"
    " where c.chunk_id = any(%(chunk_ids)s)"
)


def rrf_merge(ranked_lists: Sequence[Sequence[int]], k: int = RRF_K) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for position, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + position)
    return scores


def format_citation(hit: SearchHit) -> str:
    details: list[str] = []
    if hit.chapter:
        details.append(hit.chapter)
    if hit.page_start is not None:
        if hit.page_end is not None and hit.page_end != hit.page_start:
            details.append(f"pages {hit.page_start}–{hit.page_end}")
        else:
            details.append(f"page {hit.page_start}")
    if details:
        return f"{hit.title} — {', '.join(details)}"
    return hit.title


def run_search(
    conn, query_text: str, query_vector: Sequence[float], top_k: int, book: str | None
) -> list[SearchHit]:
    if top_k <= 0:
        return []
    candidates = top_k * CANDIDATE_MULTIPLIER
    params: dict = {
        "query_text": query_text,
        "query_vector": vector_literal(query_vector),
        "limit": candidates,
    }
    if book:
        escaped = book.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["book"] = f"%{escaped}%"

    with conn.cursor() as cur:
        cur.execute(vector_search_sql(book_filter=bool(book)), params)
        vector_ids = [row[0] for row in cur.fetchall()]
        cur.execute(fts_search_sql(book_filter=bool(book)), params)
        fts_ids = [row[0] for row in cur.fetchall()]

    scores = rrf_merge([vector_ids, fts_ids])
    if not scores:
        return []
    top_ids = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:top_k]

    with conn.cursor() as cur:
        cur.execute(DETAIL_SQL, {"chunk_ids": top_ids})
        rows = {row[0]: row for row in cur.fetchall()}

    hits = []
    for chunk_id in top_ids:
        row = rows.get(chunk_id)
        if row is None:
            continue
        hits.append(
            SearchHit(
                chunk_id=row[0],
                source_id=row[1],
                title=row[2],
                author=row[3],
                chapter=row[4],
                page_start=row[5],
                page_end=row[6],
                content=row[7],
                score=round(scores[chunk_id], 6),
            )
        )
    return hits


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="natural-language question")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--book", default=None, help="filter by source title substring")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    config = load_config()

    import psycopg

    try:
        with psycopg.connect(
            args.database_url, options="-c default_transaction_read_only=on"
        ) as conn:
            embed = build_embedder(config.embedding_model, config.batch_size)
            query_vector = embed([config.query_prefix + args.query])[0]
            hits = run_search(conn, args.query, query_vector, args.top_k, args.book)
    except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable):
        print(
            "ERROR: knowledge.chunks has no embedding/tsv columns."
            " Apply migrations/014_knowledge_embeddings.sql first.",
            file=sys.stderr,
        )
        return 2
    except psycopg.errors.UndefinedObject:
        print(
            "ERROR: pgvector extension missing."
            " Run scripts/install_pgvector.sh, then migration 014.",
            file=sys.stderr,
        )
        return 2
    except psycopg.OperationalError as exc:
        print(f"ERROR: cannot connect to database: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps([dataclasses.asdict(hit) for hit in hits], ensure_ascii=False))
        return 0

    if not hits:
        print("No matches. Has ingest_books.py been run?")
        return 0
    for rank, hit in enumerate(hits, start=1):
        print(f"{rank}. {format_citation(hit)}  [chunk {hit.chunk_id}, score {hit.score}]")
        print(f"   {hit.content[:600]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
