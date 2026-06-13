#!/usr/bin/env python3
"""Backfill embeddings for knowledge.chunks rows that have none.

One-off companion to ingest_books.py for chunks registered by earlier
pipelines. Research-only; reads chunk text and writes embeddings only.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ingest_books import (
    DEFAULT_DATABASE_URL,
    build_embedder,
    connect,
    finish_run,
    load_config,
    start_run,
    vector_literal,
)


def main() -> int:
    config = load_config()
    embed = build_embedder(config.embedding_model, config.batch_size)
    with connect(DEFAULT_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select chunk_id, content from knowledge.chunks"
                " where embedding is null order by chunk_id"
            )
            rows = cur.fetchall()
        print(f"backfilling {len(rows)} chunks", flush=True)
        if not rows:
            print("BACKFILL_DONE", flush=True)
            return 0
        run_id = start_run(conn, config)
        conn.commit()
        done = 0
        try:
            for i in range(0, len(rows), config.batch_size):
                part = rows[i : i + config.batch_size]
                vectors = embed([content for _, content in part])
                with conn.cursor() as cur:
                    for (chunk_id, _), vector in zip(part, vectors):
                        cur.execute(
                            "update knowledge.chunks set embedding = %s::vector"
                            " where chunk_id = %s",
                            (vector_literal(vector), chunk_id),
                        )
                conn.commit()
                done += len(part)
                if done % 320 == 0 or done == len(rows):
                    print(f"{done}/{len(rows)}", flush=True)
            summary = {
                "processed": [
                    {"book": "embedding_backfill", "status": "backfilled", "chunks": done}
                ],
                "failed": [],
                "skipped": [],
            }
            finish_run(conn, run_id, summary, "success")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            finish_run(
                conn,
                run_id,
                {
                    "processed": [],
                    "failed": [{"book": "embedding_backfill", "error": str(exc)}],
                    "skipped": [],
                },
                "error",
                str(exc),
            )
            conn.commit()
            raise
    print("BACKFILL_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
