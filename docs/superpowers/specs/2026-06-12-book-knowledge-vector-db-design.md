# Book Knowledge Vector Database — Design

**Date:** 2026-06-12
**Status:** Approved design, pending implementation
**Owner decisions:** runs on the VPS (`/opt/data/finance-db`), local embeddings, CLI access for Hermes, pipeline + workflow playbook scope.

## Goal

Turn trading books (PDFs dropped into a `books/` folder) into a searchable,
citable knowledge base inside the existing `finance_tracker` PostgreSQL
database, and give the Hermes agent (the Claude agent operating the VPS) a
documented procedure to go from retrieved book knowledge to a shortlist of
live-trading candidates using the repo's existing backtest and paper-trade
engines.

This implements Phase 3 ("Book ingestion pipeline") and the embedding layer
("`knowledge.embeddings` or external vector index later") of
`docs/plans/trading-knowledge-algo-plan.md`. The `knowledge` schema tables
(`sources`, `chunks`, `concepts`, `rules`, `playbooks`) already exist
(migration `001`).

## Non-goals / safety invariants

- No live order placement. Paper-only enforcement, config validation, and the
  read-only dashboard role are untouched.
- No autonomous strategy-to-live promotion: the playbook ends at a shortlist
  presented for explicit human approval.
- No LLM-based rule auto-extraction in this phase (that was the "automation
  scripts" scope option, declined). Rule/playbook rows stay human/Hermes
  curated.
- Scanned (image-only) PDFs are flagged for OCR, not OCR'd in this phase.

## Architecture decision

**pgvector inside the existing Postgres 17** (Option A), with hybrid
retrieval (vector similarity + Postgres full-text search, reciprocal-rank
fusion). Rejected alternatives:

- File-based vector store (LanceDB/Chroma): splits the source of truth,
  breaks the repo's "PostgreSQL is the source of truth" convention.
- FTS-only: misses conceptual queries; semantic search is the point.

**Embeddings:** `BAAI/bge-small-en-v1.5` (384-dim) via `sentence-transformers`
on CPU. Free, offline, books never leave the server. Model name and dimension
are config values, not hardcoded.

## Components

### 1. Migration `migrations/014_knowledge_embeddings.sql`

Idempotent, applied via the existing `psql.sh` flow:

- `create extension if not exists vector;`
- `alter table knowledge.chunks add column if not exists embedding vector(384);`
- `alter table knowledge.chunks add column if not exists tsv tsvector
  generated always as (to_tsvector('english', content)) stored;`
- HNSW index on `embedding` (cosine), GIN index on `tsv`.
- New audit table `knowledge.embedding_runs`: `run_id`, `model_name`,
  `embedding_dim`, `sources_processed`, `chunks_embedded`, `sources_skipped`,
  `status` (`running`/`success`/`error`), `error_text`, `started_at`,
  `finished_at`, `raw jsonb`.
- Grant `select` on new objects to `dashboard_ro` consistent with migration 009.

### 2. Config `config/knowledge_ingestion.json`

```json
{
  "books_dir": "books",
  "embedding_model": "BAAI/bge-small-en-v1.5",
  "embedding_dim": 384,
  "chunk_target_tokens": 800,
  "chunk_overlap_tokens": 120,
  "min_chars_per_page_for_text_pdf": 200,
  "batch_size": 32
}
```

All thresholds config-driven per repo convention. `books_dir` is relative to
the repo root locally and resolves to `/opt/data/finance-db/books/` on the VPS.

### 3. Ingestion CLI `scripts/ingest_books.py`

`uv run python scripts/ingest_books.py [--books-dir books] [--dry-run]`

Pipeline per PDF:

1. **Hash & register** — SHA-256 of file bytes. If hash already in
   `knowledge.sources`, skip (idempotent re-runs). Changed file (same
   `file_path`, new hash) ⇒ delete the old source row (chunks cascade) and
   insert a fresh one.
2. **Extract** — PyMuPDF per-page text with page numbers. Chapter headings
   detected heuristically (font-size outliers / TOC when present); chapter is
   nullable — absence is fine.
3. **Scanned detection** — if median chars/page < `min_chars_per_page_for_text_pdf`,
   the book is registered with `raw.needs_ocr = true`, reported, and skipped.
4. **Chunk** — ~`chunk_target_tokens` tokens with `chunk_overlap_tokens`
   overlap (whitespace-token approximation), never crossing detected chapter
   boundaries; rows into `knowledge.chunks` with `page_start`/`page_end`,
   `chapter`, `token_count`.
5. **Embed** — sentence-transformers in batches of `batch_size`; write
   `embedding` per chunk. Per-book transaction: an embedding failure rolls
   back that book's chunks (no half-embedded books). Other books in the batch
   continue.
6. **Audit** — one `knowledge.embedding_runs` row per invocation; per-book
   outcome in its `raw` payload.

Error handling: corrupt/encrypted/unreadable PDFs are reported and skipped,
never abort the batch. Exit code non-zero if any book failed, zero if all
succeeded or skipped-as-duplicate.

Style: `from __future__ import annotations`, dataclasses, full type hints,
`psycopg`, no FYERS/LLM calls, secrets only via `.env`.

### 4. Search CLI `scripts/query_knowledge.py`

`uv run python scripts/query_knowledge.py "trailing stops for long options" [--top-k 8] [--book <title-substring>] [--json]`

- Embeds the query with the same model, runs:
  - vector: `order by embedding <=> $query limit N`
  - FTS: `ts_rank` over `tsv` with `websearch_to_tsquery`
  - merges via reciprocal-rank fusion (k=60), returns top-k.
- Human output: ranked chunks, each cited `Title — chapter, pages X–Y
  (chunk_id N, score S)` followed by the chunk text.
- `--json`: array of `{chunk_id, source_id, title, author, chapter,
  page_start, page_end, content, score}` for Hermes to parse.
- Strictly read-only (SELECT only). Fails loudly with a clear message if the
  `vector` extension or embeddings are missing ("run ingest_books.py first").

### 5. Hermes playbook `docs/plans/book-to-live-strategy-playbook.md`

Procedure document (procedures, not book text — per the plan's own rule):

1. **Retrieve** — query the knowledge base; collect chunks with citations.
2. **Curate** — write/extend `knowledge.rules` rows (status `draft` →
   `reviewed`/`accepted` after human review) referencing `chunk_id`s.
3. **Card** — write a strategy card in `docs/strategy-cards/` following the
   existing card format, citing book/page.
4. **Hypothesize** — insert `research.hypotheses` linking `source_rule_ids`.
5. **Spec & backtest** — implement config-driven strategy; run via the
   existing backtest runners; metrics + trades into `research.backtest_runs`
   / `research.backtest_trades`. Note: proxy backtests (index-move P&L) per
   repo reality.
6. **Robustness** — parameter sensitivity, costs/slippage, out-of-sample
   split; record in run `notes`/`raw`.
7. **Paper trade** — run under the existing paper engines; decisions logged
   to `research.option_*` tables as today.
8. **Shortlist** — score via `research.latest_strategy_metrics` plus paper
   results; produce a ranked shortlist report in `reports/`.
9. **Gate** — present shortlist to the user. Live trading remains
   human-approved; nothing in this pipeline writes to `trading.execution_log`.

### 6. Deploy script `scripts/install_pgvector.sh`

Idempotent VPS-side helper: fetches the Debian `postgresql-17-pgvector`
package, extracts `vector.so` into `$PGROOT`'s lib dir and the extension
SQL/control files into the matching share dir (the custom extracted-pgroot
layout from `scripts/pg-env.sh`), then verifies with
`select * from pg_available_extensions where name='vector'`. No-op if already
installed.

### 7. Tests

- `tests/test_ingest_books.py` — chunking (sizes, overlap, chapter
  boundaries, page attribution), scanned-PDF detection, hash idempotency
  decision logic. No DB, no model download: embedding + DB layers injected/
  faked, sample text fixtures inline.
- `tests/test_query_knowledge.py` — reciprocal-rank fusion correctness,
  citation formatting, `--json` shape, read-only SQL (no
  INSERT/UPDATE/DELETE in emitted SQL).

### 8. Dependencies

Add to `pyproject.toml`: `pymupdf`, `sentence-transformers` (brings torch
CPU). Keep them in the main dependency group; document the ~500MB first-run
model/dep footprint in README.

## Data flow

```
books/*.pdf
  └─ ingest_books.py ──► knowledge.sources (hash-keyed)
                     ──► knowledge.chunks (text + pages + chapter)
                     ──► knowledge.chunks.embedding (bge-small, 384d)
                     ──► knowledge.embedding_runs (audit)

Hermes ──► query_knowledge.py "question" --json
       ◄── ranked chunks with citations
       ──► knowledge.rules / strategy card / research.hypotheses
       ──► existing backtest + paper engines
       ──► shortlist report ──► human approval gate
```

## Acceptance criteria

- `uv run pytest -q` green, including the two new test files.
- Dropping two PDFs into `books/` and running `ingest_books.py` twice
  produces each book once (second run reports skips).
- `query_knowledge.py "position sizing" --json` returns top-k chunks with
  correct page citations from the ingested books.
- Migration applies cleanly on a database that already has migrations 001–012.
- No config or code path enables live orders; dashboard role remains
  SELECT-only.
