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


def test_chunk_pages_empty_input_returns_no_chunks() -> None:
    assert ingest.chunk_pages([], target_tokens=800, overlap_tokens=120) == []


def test_detect_chapter_title_rejects_prose_starting_with_part_or_chapter() -> None:
    assert ingest.detect_chapter_title("Part civil war analogies") is None
    assert ingest.detect_chapter_title("Chapter vital risk concepts") is None


def test_detect_chapter_title_accepts_roman_numerals() -> None:
    assert ingest.detect_chapter_title("Chapter IV: Risk") == "Chapter IV Risk"
    assert ingest.detect_chapter_title("Part X Money Management") == "Part X Money Management"


def test_vector_literal_rejects_non_finite_values() -> None:
    import pytest

    with pytest.raises(ValueError):
        ingest.vector_literal([0.1, float("nan")])
