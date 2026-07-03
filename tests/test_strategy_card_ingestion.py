from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import ingest_book_strategy_cards as ingest


def test_split_text_into_chunks_preserves_page_ranges_and_overlap() -> None:
    text = "## Page 1\nBreakouts can continue when volume expands.\n" + ("trend pullback entry. " * 120) + "\n## Page 2\nFailed breakout reversal risk."

    chunks = ingest.split_text_into_chunks(text, max_chars=500, overlap_chars=80)

    assert len(chunks) > 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 2
    assert all(chunk.content for chunk in chunks)
    assert chunks[1].content[:120].strip()


def test_extract_strategy_cards_finds_expected_book_derived_setups() -> None:
    chunks = [
        ingest.TextChunk(
            chunk_index=0,
            content="Breakout continuation above resistance with expanding volume and trend context. Failed breakout reverses when price cannot hold above the level.",
            page_start=10,
            page_end=11,
            chapter=None,
            section=None,
        ),
        ingest.TextChunk(
            chunk_index=1,
            content="Randomness, survivorship bias, overfitting, and volatility regime filters should limit position sizing and prevent fragile bets.",
            page_start=22,
            page_end=22,
            chapter=None,
            section=None,
        ),
    ]

    cards = ingest.extract_strategy_cards({"Sample Trading Book": chunks})
    names = {card.name for card in cards}

    assert "Breakout Continuation" in names
    assert "Failed Breakout Reversal" in names
    assert "Randomness / Overfitting Risk Filter" in names
    assert all(card.entry_rules for card in cards)
    assert all(card.invalidation_rules for card in cards)
    assert all(card.risk_rules for card in cards)
    assert all(card.source_refs for card in cards)


def test_strategy_card_filename_replaces_path_separators() -> None:
    assert ingest.strategy_card_filename("Randomness / Overfitting Risk Filter") == "Randomness - Overfitting Risk Filter.md"


def test_render_strategy_card_markdown_is_auditable_and_non_executable() -> None:
    card = ingest.StrategyCard(
        card_id="breakout_continuation",
        name="Breakout Continuation",
        description="Test setup",
        source_refs=["Book A pp. 10-11"],
        market_regime="trend / momentum",
        timeframe="daily / intraday confirmation",
        entry_rules=["Close/hold above breakout level"],
        exit_rules=["Exit at target or failed hold"],
        invalidation_rules=["Reject stale or failed breakout"],
        risk_rules=["Cap risk before entry"],
        confidence=0.55,
        evidence=["Breakout continuation with volume expansion"],
        tags=["breakout"],
    )

    text = ingest.render_strategy_card_markdown(card)

    assert "# Breakout Continuation" in text
    assert "No live order may be placed" in text
    assert "Book A pp. 10-11" in text
    assert "Close/hold above breakout level" in text
