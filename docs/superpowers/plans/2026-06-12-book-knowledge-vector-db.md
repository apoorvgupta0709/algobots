# Book Knowledge Vector Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest trading-book PDFs from `books/` into the existing `knowledge` schema with local embeddings, and give Hermes a hybrid-search CLI plus an operating playbook from book knowledge to a live-trading shortlist.

**Architecture:** pgvector inside the existing PostgreSQL 17 (`knowledge.chunks` gains an `embedding vector(384)` column and a generated `tsv` column); a PyMuPDF→chunk→sentence-transformers ingestion CLI; a read-only hybrid (vector + FTS, reciprocal-rank-fusion) search CLI; a procedure doc for Hermes. Spec: `docs/superpowers/specs/2026-06-12-book-knowledge-vector-db-design.md`.

**Tech Stack:** Python 3.11 + uv, psycopg, PyMuPDF (`pymupdf`), `sentence-transformers` (BAAI/bge-small-en-v1.5, CPU), PostgreSQL 17 + pgvector, pytest.

**Conventions that bind every task:** `from __future__ import annotations`, dataclasses, full type hints, config-driven thresholds (no hardcoded numbers in logic), no FYERS/LLM calls anywhere in this feature, no live-order code, tests need no DB and no model download. Run all commands from the repo root: `/Users/apoorvgupta/Desktop/Itarang Files/itarang code/algobot`.

**Note on migration numbering:** the latest migration is `012_nse_intraday_options_strategy_pack.sql`, so the new migration is **014** (013 is taken by a parallel options-chain work-in-progress in this checkout) (the spec text says 012; Task 2 fixes the spec).

---

### Task 1: Dependencies + ingestion config

**Files:**
- Modify: `pyproject.toml`
- Create: `config/knowledge_ingestion.json`

- [ ] **Step 1: Add dependencies to `pyproject.toml`**

Change the `dependencies` list to:

```toml
dependencies = [
  "fyers-apiv3>=3.1.7",
  "psycopg[binary]>=3.2.0",
  "pymupdf>=1.24.0",
  "python-dotenv>=1.0.0",
  "sentence-transformers>=3.0.0",
  "streamlit>=1.35,<2",
]
```

- [ ] **Step 2: Create `config/knowledge_ingestion.json`**

```json
{
  "books_dir": "books",
  "embedding_model": "BAAI/bge-small-en-v1.5",
  "embedding_dim": 384,
  "chunk_target_tokens": 800,
  "chunk_overlap_tokens": 120,
  "min_chars_per_page_for_text_pdf": 200,
  "batch_size": 32,
  "query_prefix": "Represent this sentence for searching relevant passages: "
}
```

(`query_prefix` is the bge-family recommended instruction for query-side embedding; passages are embedded without a prefix.)

- [ ] **Step 3: Sync and verify the environment resolves**

Run: `uv sync --group dev`
Expected: resolves and installs `pymupdf`, `sentence-transformers`, `torch` (CPU) without error. First sync downloads ~500MB.

Run: `uv run python -c "import fitz, sentence_transformers; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock config/knowledge_ingestion.json
git commit -m "feat: add knowledge ingestion config and embedding deps"
```

---

### Task 2: Migration 014 — pgvector + FTS on knowledge.chunks

**Files:**
- Create: `migrations/014_knowledge_embeddings.sql`
- Modify: `docs/superpowers/specs/2026-06-12-book-knowledge-vector-db-design.md` (012 → 014)

- [ ] **Step 1: Write the migration**

Create `migrations/014_knowledge_embeddings.sql`:

```sql
-- Phase 3 book knowledge: pgvector embeddings + full-text search on chunks.
-- Research-only. No FYERS access, no live order placement code.
-- Requires the pgvector extension binaries (scripts/install_pgvector.sh on the VPS).

create extension if not exists vector;

alter table knowledge.chunks
    add column if not exists embedding vector(384);

alter table knowledge.chunks
    add column if not exists tsv tsvector
    generated always as (to_tsvector('english', content)) stored;

create index if not exists chunks_embedding_hnsw_idx
    on knowledge.chunks using hnsw (embedding vector_cosine_ops);

create index if not exists chunks_tsv_gin_idx
    on knowledge.chunks using gin (tsv);

create table if not exists knowledge.embedding_runs (
    run_id bigserial primary key,
    model_name text not null,
    embedding_dim integer not null,
    sources_processed integer not null default 0,
    chunks_embedded integer not null default 0,
    chunks_skipped integer not null default 0,
    status text not null default 'running' check (status in ('running', 'success', 'error')),
    error_text text,
    raw jsonb not null default '{}'::jsonb,
    started_at timestamptz not null default now(),
    finished_at timestamptz
);

-- Keep the read-only dashboard role working if it exists (migration 009).
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'dashboard_ro') then
        grant usage on schema knowledge to dashboard_ro;
        grant select on all tables in schema knowledge to dashboard_ro;
    end if;
end
$$;
```

- [ ] **Step 2: Fix the migration number in the spec**

In `docs/superpowers/specs/2026-06-12-book-knowledge-vector-db-design.md`, replace the string `012_knowledge_embeddings.sql` with `014_knowledge_embeddings.sql` (one occurrence in the "Components" section heading; also replace the bare reference `migration `012`` if present elsewhere).

- [ ] **Step 3: Syntax-check the migration if a local Postgres is available**

If local Postgres is running (`./scripts/start-postgres.sh` works in this checkout):
Run: `./scripts/psql.sh -f migrations/014_knowledge_embeddings.sql`
Expected: completes without error (or fails ONLY on `create extension vector` if pgvector binaries are absent locally — that is acceptable; the VPS install is Task 3).

If no local Postgres: skip — the file is plain idempotent DDL matching migration 001's style; it gets applied on the VPS at deploy time.

- [ ] **Step 4: Commit**

```bash
git add migrations/014_knowledge_embeddings.sql docs/superpowers/specs/2026-06-12-book-knowledge-vector-db-design.md
git commit -m "feat: add migration 014 - pgvector embeddings + FTS for knowledge.chunks"
```

---

### Task 3: pgvector install helper for the VPS

**Files:**
- Create: `scripts/install_pgvector.sh`

- [ ] **Step 1: Write the script**

Create `scripts/install_pgvector.sh` (mode 755). It targets the VPS's extracted-pgroot layout defined in `scripts/pg-env.sh` (`PGROOT=/opt/data/finance-db/pgroot`):

```bash
#!/usr/bin/env bash
# Idempotent pgvector install for the extracted-pgroot PostgreSQL 17 deploy.
# Run ON THE VPS as a user that can read apt archives. Read-only against the DB
# except for making the extension available; migration 014 actually creates it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pg-env.sh
source "$SCRIPT_DIR/pg-env.sh"

available() {
  "$PGBIN/psql" -tAc "select count(*) from pg_available_extensions where name = 'vector'" 2>/dev/null | grep -q '^1$'
}

if available; then
  echo "pgvector already available — nothing to do"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

echo "Downloading postgresql-17-pgvector..."
apt-get download postgresql-17-pgvector

dpkg-deb -x postgresql-17-pgvector_*.deb extracted

install -d "$PGROOT/usr/lib/postgresql/17/lib" "$PGROOT/usr/share/postgresql/17/extension"
cp extracted/usr/lib/postgresql/17/lib/vector*.so "$PGROOT/usr/lib/postgresql/17/lib/"
cp extracted/usr/share/postgresql/17/extension/vector* "$PGROOT/usr/share/postgresql/17/extension/"

if available; then
  echo "pgvector installed into $PGROOT"
else
  echo "ERROR: pgvector still not visible to PostgreSQL — check PGROOT layout" >&2
  exit 1
fi
```

Fallback note (document only, do not script): if `apt-get download` cannot find the package, add the PGDG apt repo first (`https://wiki.postgresql.org/wiki/Apt`) and retry.

- [ ] **Step 2: Make it executable and lint-check**

Run: `chmod +x scripts/install_pgvector.sh && bash -n scripts/install_pgvector.sh && echo SYNTAX_OK`
Expected: `SYNTAX_OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/install_pgvector.sh
git commit -m "feat: add idempotent pgvector install helper for VPS deploy"
```

---

### Task 4: Book ingestion CLI (`scripts/ingest_books.py`)

**Files:**
- Test: `tests/test_ingest_books.py`
- Create: `scripts/ingest_books.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingest_books.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import ingest_books as ingest


def test_load_config_matches_repo_config_file() -> None:
    config = ingest.load_config()
    assert config.embedding_model == "BAAI/bge-small-en-v1.5"
    assert config.embedding_dim == 384
    assert 0 < config.chunk_overlap_tokens < config.chunk_target_tokens
    assert config.books_dir == "books"


def test_chunk_pages_respects_target_size_and_page_attribution() -> None:
    pages = [
        ingest.PageText(page_number=1, text="alpha " * 300),
        ingest.PageText(page_number=2, text="beta " * 300),
    ]
    chunks = ingest.chunk_pages(pages, target_tokens=200, overlap_tokens=50)

    assert len(chunks) >= 3
    assert all(chunk.token_count <= 200 for chunk in chunks)
    assert chunks[0].chunk_index == 0
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 2
    assert all(chunk.content.strip() for chunk in chunks)


def test_chunk_pages_overlap_repeats_tail_words() -> None:
    words = [f"w{i}" for i in range(400)]
    pages = [ingest.PageText(page_number=1, text=" ".join(words))]
    chunks = ingest.chunk_pages(pages, target_tokens=200, overlap_tokens=50)

    first_words = chunks[0].content.split()
    second_words = chunks[1].content.split()
    assert first_words[-50:] == second_words[:50]


def test_chunk_pages_never_crosses_chapter_boundaries() -> None:
    pages = [
        ingest.PageText(page_number=1, text="Chapter 1: Risk\n" + "risk words " * 60),
        ingest.PageText(page_number=2, text="Chapter 2: Entries\n" + "entry words " * 60),
    ]
    chunks = ingest.chunk_pages(pages, target_tokens=5000, overlap_tokens=100)

    assert len({chunk.chapter for chunk in chunks}) == 2
    for chunk in chunks:
        assert not (chunk.page_start == 1 and chunk.page_end == 2)


def test_chunk_pages_rejects_bad_parameters() -> None:
    pages = [ingest.PageText(page_number=1, text="x " * 10)]
    import pytest

    with pytest.raises(ValueError):
        ingest.chunk_pages(pages, target_tokens=0, overlap_tokens=0)
    with pytest.raises(ValueError):
        ingest.chunk_pages(pages, target_tokens=100, overlap_tokens=100)


def test_detect_chapter_title_matches_common_headings() -> None:
    assert ingest.detect_chapter_title("Chapter 7: Position Sizing\nbody text") == "Chapter 7 Position Sizing"
    assert ingest.detect_chapter_title("ordinary prose about chapter topics") is None


def test_is_scanned_pdf_flags_image_only_documents() -> None:
    empty_pages = [ingest.PageText(page_number=i, text=" ") for i in range(1, 11)]
    texty_pages = [ingest.PageText(page_number=i, text="word " * 100) for i in range(1, 11)]

    assert ingest.is_scanned_pdf(empty_pages, min_chars_per_page=200)
    assert not ingest.is_scanned_pdf(texty_pages, min_chars_per_page=200)
    assert ingest.is_scanned_pdf([], min_chars_per_page=200)


def test_classify_book_is_idempotent_on_rerun() -> None:
    existing = {"books/a.pdf": "hash-a"}

    assert ingest.classify_book("books/a.pdf", "hash-a", existing) == "skip"
    assert ingest.classify_book("books/a.pdf", "hash-new", existing) == "replace"
    assert ingest.classify_book("books/b.pdf", "hash-b", existing) == "ingest"
    assert ingest.classify_book("books/c.pdf", "hash-a", existing) == "skip_duplicate_content"


def test_vector_literal_renders_pgvector_input() -> None:
    assert ingest.vector_literal([0.1, -0.25]) == "[0.1000000,-0.2500000]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest_books.py -v`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'scripts.ingest_books'` (or attribute errors).

- [ ] **Step 3: Write the implementation**

Create `scripts/ingest_books.py`:

```python
#!/usr/bin/env python3
"""Ingest trading-book PDFs into knowledge.sources/chunks with local embeddings.

Research-only pipeline. Reads PDFs from the books directory, chunks text with
page/chapter attribution, embeds chunks with a local sentence-transformers
model (CPU), and stores everything in PostgreSQL. Book text never leaves the
machine. This script never touches FYERS and never places, modifies, or
approves any orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "knowledge_ingestion.json"
INGESTOR_NAME = "book_pdf_ingestion_v1"

CHAPTER_RE = re.compile(
    r"^\s*(chapter|part)\s+([0-9]+|[ivxlc]+)\b[.:\-\s]*(.*)$", re.IGNORECASE
)

Embedder = Callable[[Sequence[str]], list[list[float]]]


@dataclass(frozen=True)
class IngestionConfig:
    books_dir: str = "books"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    chunk_target_tokens: int = 800
    chunk_overlap_tokens: int = 120
    min_chars_per_page_for_text_pdf: int = 200
    batch_size: int = 32
    query_prefix: str = "Represent this sentence for searching relevant passages: "


@dataclass(frozen=True)
class PageText:
    page_number: int  # 1-based
    text: str


@dataclass(frozen=True)
class BookChunk:
    chunk_index: int
    content: str
    page_start: int
    page_end: int
    chapter: str | None
    token_count: int


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> IngestionConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    return IngestionConfig(**data)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def detect_chapter_title(page_text: str) -> str | None:
    for line in page_text.splitlines()[:8]:
        match = CHAPTER_RE.match(line.strip())
        if match:
            parts = [part.strip() for part in match.groups() if part and part.strip()]
            return " ".join(parts)[:200]
    return None


def assign_chapters(pages: Sequence[PageText]) -> list[str | None]:
    chapters: list[str | None] = []
    current: str | None = None
    for page in pages:
        title = detect_chapter_title(page.text)
        if title is not None:
            current = title
        chapters.append(current)
    return chapters


def is_scanned_pdf(pages: Sequence[PageText], min_chars_per_page: int) -> bool:
    if not pages:
        return True
    counts = [len(page.text.strip()) for page in pages]
    return statistics.median(counts) < min_chars_per_page


def chunk_pages(
    pages: Sequence[PageText], target_tokens: int, overlap_tokens: int
) -> list[BookChunk]:
    if target_tokens <= 0:
        raise ValueError("target_tokens must be positive")
    if not 0 <= overlap_tokens < target_tokens:
        raise ValueError("overlap_tokens must be >= 0 and < target_tokens")

    chapters = assign_chapters(pages)
    groups: list[tuple[str | None, list[PageText]]] = []
    for page, chapter in zip(pages, chapters):
        if groups and groups[-1][0] == chapter:
            groups[-1][1].append(page)
        else:
            groups.append((chapter, [page]))

    chunks: list[BookChunk] = []
    index = 0
    for chapter, group_pages in groups:
        words: list[tuple[str, int]] = []
        for page in group_pages:
            words.extend((word, page.page_number) for word in page.text.split())
        start = 0
        while start < len(words):
            window = words[start : start + target_tokens]
            content = " ".join(word for word, _ in window).strip()
            if content:
                chunks.append(
                    BookChunk(
                        chunk_index=index,
                        content=content,
                        page_start=window[0][1],
                        page_end=window[-1][1],
                        chapter=chapter,
                        token_count=len(window),
                    )
                )
                index += 1
            if start + target_tokens >= len(words):
                break
            start += target_tokens - overlap_tokens
    return chunks


def classify_book(file_path: str, file_hash: str, existing: dict[str, str]) -> str:
    """Decide what to do for one PDF given registered sources (path -> hash)."""
    if existing.get(file_path) == file_hash:
        return "skip"
    if file_hash in existing.values():
        return "skip_duplicate_content"
    if file_path in existing:
        return "replace"
    return "ingest"


def extract_pages(pdf_path: Path) -> list[PageText]:
    import fitz  # PyMuPDF

    pages: list[PageText] = []
    with fitz.open(pdf_path) as doc:
        if doc.is_encrypted and not doc.authenticate(""):
            raise ValueError(f"encrypted PDF: {pdf_path.name}")
        for number, page in enumerate(doc, start=1):
            pages.append(PageText(page_number=number, text=page.get_text("text")))
    return pages


def build_embedder(model_name: str, batch_size: int) -> Embedder:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name, device="cpu")

    def embed(texts: Sequence[str]) -> list[list[float]]:
        vectors = model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [vector.tolist() for vector in vectors]

    return embed


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


def connect(database_url: str):
    import psycopg

    return psycopg.connect(database_url)


def fetch_existing_sources(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "select file_path, file_hash from knowledge.sources"
            " where file_path is not null and file_hash is not null"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def start_run(conn, config: IngestionConfig) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into knowledge.embedding_runs (model_name, embedding_dim)"
            " values (%s, %s) returning run_id",
            (config.embedding_model, config.embedding_dim),
        )
        return cur.fetchone()[0]


def finish_run(
    conn, run_id: int, summary: dict, status: str, error_text: str | None = None
) -> None:
    from psycopg.types.json import Jsonb

    chunks_embedded = sum(item.get("chunks", 0) for item in summary["processed"])
    with conn.cursor() as cur:
        cur.execute(
            "update knowledge.embedding_runs"
            " set status = %s, error_text = %s, sources_processed = %s,"
            "     chunks_embedded = %s, chunks_skipped = %s, raw = %s,"
            "     finished_at = now()"
            " where run_id = %s",
            (
                status,
                error_text,
                len(summary["processed"]),
                chunks_embedded,
                len(summary["skipped"]),
                Jsonb(summary),
                run_id,
            ),
        )


def ingest_one_book(
    conn,
    pdf_path: Path,
    rel_path: str,
    file_hash: str,
    action: str,
    config: IngestionConfig,
    embed: Embedder,
) -> dict:
    from psycopg.types.json import Jsonb

    title = pdf_path.stem
    pages = extract_pages(pdf_path)

    if is_scanned_pdf(pages, config.min_chars_per_page_for_text_pdf):
        with conn.cursor() as cur:
            if action == "replace":
                cur.execute(
                    "delete from knowledge.sources where file_path = %s", (rel_path,)
                )
            cur.execute(
                "insert into knowledge.sources (title, source_type, file_path,"
                " file_hash, notes, raw) values (%s, 'book', %s, %s, %s, %s)"
                " on conflict (file_hash) do nothing",
                (
                    title,
                    rel_path,
                    file_hash,
                    "image-only PDF; needs OCR before ingestion",
                    Jsonb({"ingestor": INGESTOR_NAME, "needs_ocr": True, "pages": len(pages)}),
                ),
            )
        return {"book": title, "status": "needs_ocr", "chunks": 0}

    chunks = chunk_pages(pages, config.chunk_target_tokens, config.chunk_overlap_tokens)
    embeddings = embed([chunk.content for chunk in chunks])
    if len(embeddings) != len(chunks):
        raise RuntimeError(f"embedding count mismatch for {title}")

    with conn.cursor() as cur:
        if action == "replace":
            cur.execute(
                "delete from knowledge.sources where file_path = %s", (rel_path,)
            )
        cur.execute(
            "insert into knowledge.sources (title, source_type, file_path, file_hash, raw)"
            " values (%s, 'book', %s, %s, %s) returning source_id",
            (
                title,
                rel_path,
                file_hash,
                Jsonb({"ingestor": INGESTOR_NAME, "pages": len(pages)}),
            ),
        )
        source_id = cur.fetchone()[0]
        for chunk, embedding in zip(chunks, embeddings):
            cur.execute(
                "insert into knowledge.chunks"
                " (source_id, chunk_index, chapter, page_start, page_end,"
                "  content, token_count, embedding, raw)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
                (
                    source_id,
                    chunk.chunk_index,
                    chunk.chapter,
                    chunk.page_start,
                    chunk.page_end,
                    chunk.content,
                    chunk.token_count,
                    vector_literal(embedding),
                    Jsonb({"ingestor": INGESTOR_NAME}),
                ),
            )
    return {"book": title, "status": "ingested", "chunks": len(chunks)}


def run_ingestion(
    books_dir: Path,
    database_url: str,
    config: IngestionConfig,
    embed: Embedder | None = None,
    dry_run: bool = False,
) -> dict:
    summary: dict = {"processed": [], "failed": [], "skipped": []}
    pdf_paths = sorted(books_dir.glob("*.pdf"))
    if not pdf_paths:
        print(f"No PDFs found in {books_dir}")
        return summary

    with connect(database_url) as conn:
        existing = fetch_existing_sources(conn)
        run_id = None if dry_run else start_run(conn, config)
        if embed is None and not dry_run:
            embed = build_embedder(config.embedding_model, config.batch_size)
        try:
            for pdf_path in pdf_paths:
                rel_path = str(pdf_path.resolve())
                file_hash = sha256_file(pdf_path)
                action = classify_book(rel_path, file_hash, existing)
                if action in ("skip", "skip_duplicate_content"):
                    summary["skipped"].append({"book": pdf_path.stem, "reason": action})
                    continue
                if dry_run:
                    summary["processed"].append(
                        {"book": pdf_path.stem, "status": "dry_run", "action": action, "chunks": 0}
                    )
                    continue
                try:
                    with conn.transaction():
                        outcome = ingest_one_book(
                            conn, pdf_path, rel_path, file_hash, action, config, embed
                        )
                    summary["processed"].append(outcome)
                except Exception as exc:  # noqa: BLE001 — keep batch going
                    summary["failed"].append({"book": pdf_path.stem, "error": str(exc)})
            if run_id is not None:
                status = "success" if not summary["failed"] else "error"
                error_text = (
                    "; ".join(f"{f['book']}: {f['error']}" for f in summary["failed"])
                    or None
                )
                finish_run(conn, run_id, summary, status, error_text)
        except Exception as exc:
            if run_id is not None:
                finish_run(conn, run_id, summary, "error", str(exc))
            raise
    return summary


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--books-dir", default=None, help="override config books_dir")
    parser.add_argument(
        "--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    config = load_config(Path(args.config))
    books_dir = Path(args.books_dir or config.books_dir)
    if not books_dir.is_absolute():
        books_dir = PROJECT_ROOT / books_dir

    summary = run_ingestion(books_dir, args.database_url, config, dry_run=args.dry_run)

    for item in summary["processed"]:
        print(f"[{item['status']}] {item['book']} ({item['chunks']} chunks)")
    for item in summary["skipped"]:
        print(f"[skipped:{item['reason']}] {item['book']}")
    for item in summary["failed"]:
        print(f"[FAILED] {item['book']}: {item['error']}", file=sys.stderr)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_ingest_books.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 5: Run the whole suite to check nothing broke**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_ingest_books.py scripts/ingest_books.py
git commit -m "feat: add PDF book ingestion pipeline with local embeddings"
```

---

### Task 5: Hybrid search CLI (`scripts/query_knowledge.py`)

**Files:**
- Test: `tests/test_query_knowledge.py`
- Create: `scripts/query_knowledge.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_query_knowledge.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_query_knowledge.py -v`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'scripts.query_knowledge'`.

- [ ] **Step 3: Write the implementation**

Create `scripts/query_knowledge.py`:

```python
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
    candidates = top_k * CANDIDATE_MULTIPLIER
    params: dict = {
        "query_text": query_text,
        "query_vector": vector_literal(query_vector),
        "limit": candidates,
    }
    if book:
        params["book"] = f"%{book}%"

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
    import psycopg

    args = parse_args(argv if argv is not None else sys.argv[1:])
    config = load_config()
    embed = build_embedder(config.embedding_model, config.batch_size)
    query_vector = embed([config.query_prefix + args.query])[0]

    try:
        with psycopg.connect(
            args.database_url, options="-c default_transaction_read_only=on"
        ) as conn:
            hits = run_search(conn, args.query, query_vector, args.top_k, args.book)
    except psycopg.errors.UndefinedColumn:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_query_knowledge.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add tests/test_query_knowledge.py scripts/query_knowledge.py
git commit -m "feat: add hybrid vector+FTS knowledge search CLI"
```

---

### Task 6: Hermes operating playbook

**Files:**
- Create: `docs/plans/book-to-live-strategy-playbook.md`

- [ ] **Step 1: Write the playbook**

Create `docs/plans/book-to-live-strategy-playbook.md`:

```markdown
# Book → Live Strategy Playbook (for Hermes)

> Operating procedure for turning ingested book knowledge into a shortlist of
> live-trading candidates. Procedures only — raw book text stays in
> `knowledge.chunks`. Safety invariants from CLAUDE.md apply at every step:
> paper-only engines, no live orders, `trading.execution_log` requires an
> explicit human approval record.

## Prerequisites

- Books ingested: `uv run python scripts/ingest_books.py` (PDFs in `books/`).
- Search works: `uv run python scripts/query_knowledge.py "test" --top-k 3`.

## Step 1 — Retrieve

Query the knowledge base for the theme under research:

    uv run python scripts/query_knowledge.py "<question>" --top-k 8 --json

Collect chunk_ids and citations (title, chapter, pages) for everything you
intend to use. Never paraphrase a book without a chunk citation.

## Step 2 — Curate rules

For each actionable claim, insert a `knowledge.rules` row (status `draft`)
referencing the supporting `chunk_id` and `source_id`, with `statement`,
`market_regime`, `timeframe`, and an honest `confidence`. Statuses move
`draft → reviewed → accepted` only with human review. Only `reviewed` or
`accepted` rules may seed hypotheses.

## Step 3 — Strategy card

Write a Markdown card in `docs/strategy-cards/` following the existing card
format (see `docs/strategy-cards/Trend Pullback Entry.md`): description,
market regime, timeframe, entry/exit/invalidation/risk rules, and a Sources
section citing book + pages.

## Step 4 — Hypothesis

Insert a `research.hypotheses` row: plain-English hypothesis,
`source_rule_ids` from Step 2, target universe, timeframe, expected edge.
Status `draft → ready_for_backtest` when specified well enough to code.

## Step 5 — Specify and backtest

Implement the strategy config-driven (params in `config/*.json`, no hardcoded
thresholds), register a `research.strategy_versions` row, and run the
appropriate existing engine:

- Index-options proxy backtests: `scripts/run_banknifty_pullback_v2_backtest.py`
  pattern (P&L proxied from index moves — state this limitation in the run notes).
- Intraday packs: `scripts/run_nse_intraday_options_strategy_pack.py --mode backtest`.

Metrics and full trade lists go to `research.backtest_runs` /
`research.backtest_trades`.

## Step 6 — Robustness

Before paper: parameter sensitivity (±20% on each key param), realistic costs
and slippage, and an out-of-sample split. Record outcomes in the backtest
run's `notes`/`raw`. A strategy that only works at one parameter point is
rejected — cite `docs/strategy-cards/Randomness - Overfitting Risk Filter.md`.

## Step 7 — Paper trade

Run under the existing paper engines (`scripts/banknifty_options_paper.py`,
`scripts/run_paper_algobot.py`). Decisions log to `research.option_*` tables
as today. Minimum evaluation window before shortlisting: 4 weeks or 20 signals,
whichever comes later (adjustable by the user, not by Hermes).

## Step 8 — Shortlist

Score candidates from `research.latest_strategy_metrics` plus paper results:
net expectancy after costs, max drawdown, hit rate vs payoff, signal count,
and backtest-vs-paper consistency. Write a ranked shortlist report to
`reports/` (timestamped filename) with per-strategy evidence links back to
rules → chunks → book pages.

## Step 9 — Human gate (hard stop)

Present the shortlist to the user. Live trading requires the user's explicit
approval and the `trading.approvals` flow. Hermes never enables live orders,
never writes `trading.execution_log`, and never edits safety config. If a
task appears to require it, surface the conflict and stop.
```

- [ ] **Step 2: Commit**

```bash
git add docs/plans/book-to-live-strategy-playbook.md
git commit -m "docs: add Hermes book-to-live-strategy operating playbook"
```

---

### Task 7: README, final verification

**Files:**
- Modify: `README.md` (add a section; place it after the existing setup/usage sections, matching the README's heading style)

- [ ] **Step 1: Add a "Book knowledge base" section to README.md**

Append (or slot near the other usage sections):

```markdown
## Book knowledge base

Drop trading-book PDFs into `books/` (VPS: `/opt/data/finance-db/books/`), then:

​```bash
# one-time on the VPS: make pgvector available, then apply migration 014
./scripts/install_pgvector.sh
./scripts/psql.sh -f migrations/014_knowledge_embeddings.sql

# ingest (idempotent — re-running skips unchanged books)
uv run python scripts/ingest_books.py

# search with citations (read-only; --json for agents)
uv run python scripts/query_knowledge.py "position sizing for index options" --top-k 8
​```

Embeddings are computed locally (BAAI/bge-small-en-v1.5 on CPU; first run
downloads the model to `~/.cache`). Image-only/scanned PDFs are registered
with `needs_ocr` and skipped. Config: `config/knowledge_ingestion.json`.
Workflow from book knowledge to a live-trading shortlist:
`docs/plans/book-to-live-strategy-playbook.md`.
```

(Remove the zero-width characters around the inner code fence when pasting — they exist only to nest the fence in this plan.)

- [ ] **Step 2: Full suite + safety re-check**

Run: `uv run pytest -q`
Expected: all green.

Run: `grep -rn "live_orders_enabled" config/ | grep -v false`
Expected: no output (no config enables live orders).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document book knowledge base usage"
```

---

### Task 8 (deploy day, on the VPS — requires user-authorized SSH)

Not executable from this checkout; listed for completeness so nothing is forgotten:

1. `git pull` the repo on the VPS (`/opt/data/finance-db`), `uv sync`.
2. `./scripts/install_pgvector.sh`
3. `./scripts/psql.sh -f migrations/014_knowledge_embeddings.sql`
4. `mkdir -p /opt/data/finance-db/books` and upload PDFs.
5. `uv run python scripts/ingest_books.py`
6. Smoke test: `uv run python scripts/query_knowledge.py "risk management" --top-k 3`

---

## Acceptance criteria (from the spec)

- `uv run pytest -q` green including the two new test files.
- Running `ingest_books.py` twice over the same PDFs ingests each book once (second run reports skips).
- `query_knowledge.py "position sizing" --json` returns top-k chunks with page citations.
- Migration applies cleanly after 001–012.
- No config or code path enables live orders; dashboard role stays SELECT-only.
