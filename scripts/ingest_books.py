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
import math
import os
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "knowledge_ingestion.json"
INGESTOR_NAME = "book_pdf_ingestion_v1"

# Decimal chapter numbers, or roman numerals 1-39 (strict forms only, so prose
# like "Part civil ..." or "Chapter vital ..." never matches).
CHAPTER_RE = re.compile(
    r"^\s*(chapter|part)\s+"
    r"([0-9]{1,4}|x{1,3}(?:ix|iv|v?i{0,3})?|ix|iv|v?i{1,3}|v)"
    r"\b[.:\-\s]*(.*)$",
    re.IGNORECASE,
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

    # BGE convention: config.query_prefix is applied at retrieval time only.
    # Documents are embedded WITHOUT the prefix — do not add it here.
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
    if any(not math.isfinite(value) for value in vector):
        raise ValueError(f"embedding contains non-finite values ({len(vector)}-dim)")
    return "[" + ",".join(f"{value:.7f}" for value in vector) + "]"


def connect(database_url: str) -> psycopg.Connection:
    import psycopg

    return psycopg.connect(database_url)


def fetch_existing_sources(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "select file_path, file_hash from knowledge.sources"
            " where file_path is not null and file_hash is not null"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def start_run(conn: psycopg.Connection, config: IngestionConfig) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into knowledge.embedding_runs (model_name, embedding_dim)"
            " values (%s, %s) returning run_id",
            (config.embedding_model, config.embedding_dim),
        )
        return cur.fetchone()[0]


def finish_run(
    conn: psycopg.Connection, run_id: int, summary: dict, status: str, error_text: str | None = None
) -> None:
    from psycopg.types.json import Jsonb

    chunks_embedded = sum(item.get("chunks", 0) for item in summary["processed"])
    with conn.cursor() as cur:
        cur.execute(
            "update knowledge.embedding_runs"
            " set status = %s, error_text = %s, sources_processed = %s,"
            "     chunks_embedded = %s, sources_skipped = %s, raw = %s,"
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
    conn: psycopg.Connection,
    pdf_path: Path,
    abs_path: str,
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
                    "delete from knowledge.sources where file_path = %s", (abs_path,)
                )
            cur.execute(
                "insert into knowledge.sources (title, source_type, file_path,"
                " file_hash, notes, raw) values (%s, 'book', %s, %s, %s, %s)"
                " on conflict (file_hash) do nothing",
                (
                    title,
                    abs_path,
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
                "delete from knowledge.sources where file_path = %s", (abs_path,)
            )
        cur.execute(
            "insert into knowledge.sources (title, source_type, file_path, file_hash, raw)"
            " values (%s, 'book', %s, %s, %s) returning source_id",
            (
                title,
                abs_path,
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
        if run_id is not None:
            conn.commit()
        try:
            if embed is None and not dry_run:
                embed = build_embedder(config.embedding_model, config.batch_size)
            for pdf_path in pdf_paths:
                # Absolute path keys the source row; assumes ingestion always runs on the
                # machine that owns the books directory (the VPS).
                abs_path = str(pdf_path.resolve())
                file_hash = sha256_file(pdf_path)
                action = classify_book(abs_path, file_hash, existing)
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
                            conn, pdf_path, abs_path, file_hash, action, config, embed
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
                try:
                    conn.rollback()
                    finish_run(conn, run_id, summary, "error", str(exc))
                    conn.commit()
                except Exception as audit_exc:  # noqa: BLE001 — keep original error primary
                    print(f"WARNING: could not record run failure: {audit_exc}", file=sys.stderr)
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
