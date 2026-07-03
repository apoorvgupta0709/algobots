#!/usr/bin/env python3
"""Ingest trading-book text into knowledge chunks and draft strategy cards.

Research-only pipeline. It creates auditable chunks/rules/playbooks from the
existing trading-library text files. It does not place, modify, or approve any
orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIBRARY_ROOT = Path("/opt/data/trading-library")
DEFAULT_DATABASE_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"
EXTRACTOR_NAME = "book_strategy_card_ingestion_v1"


DEFAULT_DATABASE_URL = "host=127.0.0.1 port=55432 dbname=finance_tracker user=hermes"
EXTRACTOR_NAME = "book_strategy_card_ingestion_v1"


@dataclass(frozen=True)
class TextChunk:
    chunk_index: int
    content: str
    page_start: int | None
    page_end: int | None
    chapter: str | None = None
    section: str | None = None


@dataclass(frozen=True)
class BookText:
    title: str
    text_path: Path
    pdf_path: Path | None
    file_hash: str
    source_id: int | None
    chunks: list[TextChunk]
    metadata: dict[str, object]


@dataclass(frozen=True)
class StrategyCard:
    card_id: str
    name: str
    description: str
    source_refs: list[str]
    market_regime: str
    timeframe: str
    entry_rules: list[str]
    exit_rules: list[str]
    invalidation_rules: list[str]
    risk_rules: list[str]
    confidence: float
    evidence: list[str]
    tags: list[str]


@dataclass(frozen=True)
class StrategyTemplate:
    card_id: str
    name: str
    description: str
    keywords: tuple[str, ...]
    market_regime: str
    timeframe: str
    entry_rules: tuple[str, ...]
    exit_rules: tuple[str, ...]
    invalidation_rules: tuple[str, ...]
    risk_rules: tuple[str, ...]
    tags: tuple[str, ...]


STRATEGY_TEMPLATES: tuple[StrategyTemplate, ...] = (
    StrategyTemplate(
        card_id="breakout_continuation",
        name="Breakout Continuation",
        description="Momentum continuation setup after price breaks and holds above a prior resistance/range level with participation.",
        keywords=("breakout", "resistance", "range", "volume", "continuation", "momentum", "new high", "trend"),
        market_regime="trend / momentum expansion",
        timeframe="daily setup with intraday confirmation",
        entry_rules=(
            "Only consider long setups when price breaks or reclaims a clear resistance/range high and then holds above it.",
            "Prefer setups with trend alignment and expanding participation/volume rather than thin breakouts.",
            "Use current local FYERS/Postgres technical factors as the execution-time source of truth.",
        ),
        exit_rules=(
            "Take partial/full exit at predefined reward-to-risk target or when momentum fails.",
            "Exit if price closes back inside the old range after entry and invalidation is confirmed.",
        ),
        invalidation_rules=(
            "Reject if breakout level is not identifiable or price cannot hold above the level.",
            "Reject if quote/factor data is stale or volume participation is weak.",
        ),
        risk_rules=(
            "Stop must be below the breakout/retest level or recent swing low before any trade idea is sent.",
            "Position size must cap rupee risk; never widen stop after entry.",
        ),
        tags=("technical", "breakout", "momentum"),
    ),
    StrategyTemplate(
        card_id="failed_breakout_reversal",
        name="Failed Breakout Reversal",
        description="Reversal/avoidance setup when a breakout traps participants and fails to hold above the trigger level.",
        keywords=("failed breakout", "false breakout", "failure", "trap", "reversal", "cannot hold", "breakout", "range"),
        market_regime="range / exhaustion / trap",
        timeframe="intraday confirmation after daily context",
        entry_rules=(
            "Treat inability to hold above breakout level as a warning against fresh longs.",
            "For paper/research only, reversal ideas require confirmation back inside the prior range and clear stop placement.",
        ),
        exit_rules=(
            "Exit reversal paper trades near the opposite range boundary or when price reclaims the failed level.",
        ),
        invalidation_rules=(
            "Invalidate reversal thesis if price quickly reclaims the breakout level with volume.",
            "Do not short/exit mechanically from a single failed tick; require confirmed failure.",
        ),
        risk_rules=(
            "Keep risk small because failed-breakout trades can re-squeeze violently.",
            "No live short/derivative execution unless separately enabled and explicitly confirmed.",
        ),
        tags=("technical", "breakout", "reversal", "risk"),
    ),
    StrategyTemplate(
        card_id="trend_pullback_entry",
        name="Trend Pullback Entry",
        description="Trend-following setup that waits for a controlled pullback toward support/mean before continuation.",
        keywords=("pullback", "trend", "support", "moving average", "ema", "sma", "higher low", "continuation"),
        market_regime="established trend with controlled pullback",
        timeframe="daily trend with intraday entry timing",
        entry_rules=(
            "Only consider pullbacks when the higher timeframe trend remains intact.",
            "Prefer pullbacks toward support, EMA/SMA zones, or prior breakout levels with signs of stabilization.",
        ),
        exit_rules=(
            "Exit if price fails to resume trend or violates the pullback swing low.",
        ),
        invalidation_rules=(
            "Reject if pullback becomes a trend break, high-volume distribution, or stop reference is above entry.",
        ),
        risk_rules=(
            "Stop goes below pullback swing low/support; size from stop distance, not conviction.",
        ),
        tags=("technical", "trend", "pullback"),
    ),
    StrategyTemplate(
        card_id="volatility_regime_filter",
        name="Volatility Regime Filter",
        description="Filter that adjusts or blocks setups when volatility is too low, too high, or structurally unstable.",
        keywords=("volatility", "vol", "atr", "option", "dynamic hedging", "gamma", "skew", "regime", "variance"),
        market_regime="all regimes; used as a filter",
        timeframe="daily/intraday risk overlay",
        entry_rules=(
            "Allow setups only when ATR/realized volatility is compatible with the planned stop and target.",
            "For high-volatility regimes, demand wider confirmation and smaller size; for dead-volatility regimes, avoid low-movement setups.",
        ),
        exit_rules=(
            "Reduce/exit if volatility expands against the trade or invalidates the stop model.",
        ),
        invalidation_rules=(
            "Reject setups where volatility makes the stop unrealistic or expected movement too small.",
        ),
        risk_rules=(
            "Use volatility-adjusted sizing; never use fixed quantity without checking ATR and max rupee risk.",
        ),
        tags=("risk", "volatility", "filter"),
    ),
    StrategyTemplate(
        card_id="randomness_overfitting_risk_filter",
        name="Randomness / Overfitting Risk Filter",
        description="Research governance filter to avoid treating luck, overfit backtests, or weak samples as tradable edge.",
        keywords=("randomness", "luck", "survivorship", "overfitting", "data snooping", "backtest", "sample", "fragile", "fooled"),
        market_regime="all regimes; research validation filter",
        timeframe="pre-trade and post-trade review",
        entry_rules=(
            "A setup may be promoted only after enough sample evidence or clear discretionary rationale is recorded.",
            "Treat one-off wins as weak evidence; require repeatable conditions before increasing confidence.",
        ),
        exit_rules=(
            "Retire or downgrade rules that perform only in cherry-picked samples or fail out-of-sample/paper review.",
        ),
        invalidation_rules=(
            "Reject strategy variants that depend on hindsight, untradeable prices, stale data, or excessive parameters.",
        ),
        risk_rules=(
            "Keep new/unvalidated cards in paper mode or minimum risk until evidence improves.",
        ),
        tags=("research", "risk", "overfitting"),
    ),
    StrategyTemplate(
        card_id="position_sizing_risk",
        name="Position Sizing / Risk Rule Card",
        description="Universal risk card: trade size is derived from invalidation distance and maximum allowed loss.",
        keywords=("position sizing", "risk", "stop", "drawdown", "ruin", "loss", "discipline", "money management", "capital"),
        market_regime="all regimes; mandatory risk overlay",
        timeframe="every trade idea",
        entry_rules=(
            "No trade idea is valid until entry, stop, target, quantity, and max loss are known.",
            "Size must be computed from rupee risk per share and configured per-trade risk cap.",
        ),
        exit_rules=(
            "Exit when stop/target/time-stop triggers; do not move risk limit farther away after entry.",
        ),
        invalidation_rules=(
            "Reject if stop is missing, stop is on wrong side of entry, or quantity breaches risk config.",
        ),
        risk_rules=(
            "Respect max risk per trade, daily loss, weekly loss, max capital, and max open positions.",
            "Live execution remains disabled unless the approval gate and kill-switch checks pass.",
        ),
        tags=("risk", "sizing", "mandatory"),
    ),
    StrategyTemplate(
        card_id="earnings_sentiment_momentum",
        name="Earnings Sentiment Momentum",
        description="Context card for price strength supported by earnings/news/filing sentiment rather than technicals alone.",
        keywords=("earnings", "guidance", "sentiment", "news", "filing", "revenue", "profit", "margin", "upgrade", "downgrade"),
        market_regime="event-driven trend / post-news drift",
        timeframe="fresh event window plus daily confirmation",
        entry_rules=(
            "Require a fresh positive or improving event context before adding sentiment score.",
            "Pair sentiment with technical confirmation; do not buy solely from a headline or LLM summary.",
        ),
        exit_rules=(
            "Exit/downgrade if the catalyst is contradicted by later filings/news or price rejects the move.",
        ),
        invalidation_rules=(
            "Reject stale, unsourced, or low-quality sentiment; mark mixed/negative event risk explicitly.",
        ),
        risk_rules=(
            "Lower size around binary events and when sentiment confidence is low.",
        ),
        tags=("sentiment", "fundamental", "event"),
    ),
    StrategyTemplate(
        card_id="fundamental_quality_technical_breakout",
        name="Fundamental Quality + Technical Breakout",
        description="Multi-factor setup where quality/growth/valuation support a technical breakout candidate.",
        keywords=("fundamental", "quality", "valuation", "revenue", "profit", "margin", "roe", "roce", "growth", "breakout"),
        market_regime="quality momentum / institutional accumulation",
        timeframe="daily/weekly context with event freshness",
        entry_rules=(
            "Prefer breakouts in companies with supportive quality/growth and no major leverage or valuation red flags.",
            "Use sector-relative fundamental score once the fundamental engine is populated.",
        ),
        exit_rules=(
            "Exit/downgrade if technical breakout fails or fundamental thesis deteriorates.",
        ),
        invalidation_rules=(
            "Reject if technical setup is strong but fundamentals show severe quality/leverage/event risk.",
        ),
        risk_rules=(
            "Fundamentals improve selectivity; they do not override stop-loss and max-loss rules.",
        ),
        tags=("fundamental", "technical", "breakout"),
    ),
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].splitlines()
    metadata: dict[str, object] = {}
    for line in raw:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip('"')
        if value.isdigit():
            metadata[key.strip()] = int(value)
        else:
            metadata[key.strip()] = value
    return metadata, text[end + 5 :]


def title_from_path(path: Path) -> str:
    name = path.stem.replace("_", " ").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name or path.stem


def page_markers(text: str) -> list[tuple[int, int]]:
    markers: list[tuple[int, int]] = []
    for match in re.finditer(r"(?im)^##\s+Page\s+(\d+)\s*$", text):
        markers.append((match.start(), int(match.group(1))))
    return markers


def pages_for_span(markers: list[tuple[int, int]], start: int, end: int) -> tuple[int | None, int | None]:
    if not markers:
        return None, None
    active = [page for pos, page in markers if pos <= end]
    span_pages = [page for pos, page in markers if start <= pos <= end]
    page_start = span_pages[0] if span_pages else (active[-1] if active else markers[0][1])
    later = [page for pos, page in markers if start <= pos <= end]
    page_end = later[-1] if later else page_start
    return page_start, page_end


def split_text_into_chunks(text: str, max_chars: int = 6000, overlap_chars: int = 500) -> list[TextChunk]:
    if max_chars < 200:
        raise ValueError("max_chars must be >= 200")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be >= 0 and smaller than max_chars")

    metadata, body = parse_frontmatter(text)
    del metadata
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body:
        return []

    markers = page_markers(body)
    chunks: list[TextChunk] = []
    start = 0
    while start < len(body):
        hard_end = min(len(body), start + max_chars)
        end = hard_end
        if hard_end < len(body):
            # Prefer paragraph boundary, then line boundary, then hard cut.
            para = body.rfind("\n\n", start + max_chars // 2, hard_end)
            line = body.rfind("\n", start + max_chars // 2, hard_end)
            end = para if para != -1 else (line if line != -1 else hard_end)
        content = body[start:end].strip()
        if content:
            page_start, page_end = pages_for_span(markers, start, end)
            chunks.append(TextChunk(len(chunks), content, page_start, page_end))
        if end >= len(body):
            break
        start = max(0, end - overlap_chars)
    return chunks


def load_book_texts(library_root: Path = DEFAULT_LIBRARY_ROOT, limit: int | None = None) -> list[BookText]:
    text_dir = library_root / "books" / "text"
    if not text_dir.exists():
        raise FileNotFoundError(f"Text directory not found: {text_dir}")
    books: list[BookText] = []
    for text_path in sorted(text_dir.glob("*.md")):
        raw_text = text_path.read_text(encoding="utf-8", errors="replace")
        metadata, _ = parse_frontmatter(raw_text)
        title = str(metadata.get("title") or title_from_path(text_path))
        pdf_raw = str(metadata.get("pdf_file") or "").strip()
        pdf_path = Path(pdf_raw) if pdf_raw else None
        if pdf_path is not None and not pdf_path.exists():
            pdf_path = None
        hash_path = pdf_path or text_path
        chunks = split_text_into_chunks(raw_text)
        books.append(
            BookText(
                title=title,
                text_path=text_path,
                pdf_path=pdf_path,
                file_hash=sha256_file(hash_path),
                source_id=None,
                chunks=chunks,
                metadata=metadata,
            )
        )
        if limit is not None and len(books) >= limit:
            break
    return books


def normalized_keyword_score(text: str, keywords: Sequence[str]) -> int:
    lowered = text.lower()
    return sum(1 for keyword in keywords if keyword.lower() in lowered)


def compact_evidence(text: str, keywords: Sequence[str], max_len: int = 360) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    lowered = normalized.lower()
    positions = [lowered.find(k.lower()) for k in keywords if lowered.find(k.lower()) >= 0]
    if not positions:
        return normalized[:max_len]
    center = min(positions)
    start = max(0, center - max_len // 3)
    snippet = normalized[start : start + max_len]
    return snippet.strip(" .,;:-")


def extract_strategy_cards(book_chunks: dict[str, list[TextChunk]], max_evidence_per_card: int = 6) -> list[StrategyCard]:
    cards: list[StrategyCard] = []
    for template in STRATEGY_TEMPLATES:
        hits: list[tuple[int, str, TextChunk]] = []
        for title, chunks in book_chunks.items():
            for chunk in chunks:
                score = normalized_keyword_score(chunk.content, template.keywords)
                if score > 0:
                    hits.append((score, title, chunk))
        hits.sort(key=lambda item: (item[0], len(item[2].content)), reverse=True)
        selected = hits[:max_evidence_per_card]
        if not selected:
            continue
        source_refs: list[str] = []
        evidence: list[str] = []
        for _score, title, chunk in selected:
            if chunk.page_start is not None and chunk.page_end is not None:
                page_ref = f"pp. {chunk.page_start}-{chunk.page_end}" if chunk.page_start != chunk.page_end else f"p. {chunk.page_start}"
            else:
                page_ref = f"chunk {chunk.chunk_index}"
            ref = f"{title} {page_ref}"
            if ref not in source_refs:
                source_refs.append(ref)
            snippet = compact_evidence(chunk.content, template.keywords)
            if snippet and snippet not in evidence:
                evidence.append(snippet)
        confidence = min(0.85, 0.45 + (0.05 * len(selected)) + (0.03 * min(sum(score for score, _, _ in selected), 5)))
        cards.append(
            StrategyCard(
                card_id=template.card_id,
                name=template.name,
                description=template.description,
                source_refs=source_refs,
                market_regime=template.market_regime,
                timeframe=template.timeframe,
                entry_rules=list(template.entry_rules),
                exit_rules=list(template.exit_rules),
                invalidation_rules=list(template.invalidation_rules),
                risk_rules=list(template.risk_rules),
                confidence=round(confidence, 4),
                evidence=evidence,
                tags=list(template.tags),
            )
        )
    return cards


def render_bullets(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def render_strategy_card_markdown(card: StrategyCard) -> str:
    return f"""# {card.name}

## Status
- Status: draft / research-only
- Confidence: {card.confidence:.2f}
- Market regime: {card.market_regime}
- Timeframe: {card.timeframe}
- Safety: No live order may be placed from this card without Apoorv's exact Telegram confirmation and live-gate risk checks.

## Description
{card.description}

## Source references
{render_bullets(card.source_refs)}

## Entry rules
{render_bullets(card.entry_rules)}

## Exit rules
{render_bullets(card.exit_rules)}

## Invalidation rules
{render_bullets(card.invalidation_rules)}

## Risk rules
{render_bullets(card.risk_rules)}

## Evidence snippets
{render_bullets(card.evidence)}

## Tags
{', '.join(card.tags)}

## Processing checklist
- [ ] Review source evidence manually
- [ ] Convert to explicit hypothesis
- [ ] Backtest on local market data
- [ ] Paper trade with journal
- [ ] Promote only after performance review
"""


def strategy_card_filename(name: str) -> str:
    safe = re.sub(r"[\\/]+", " - ", name).strip()
    safe = re.sub(r"\s+", " ", safe)
    safe = re.sub(r"[^\w\s().,&+-]", "", safe).strip(" .")
    return f"{safe or 'Strategy Card'}.md"


def write_obsidian_cards(cards: Sequence[StrategyCard], library_root: Path = DEFAULT_LIBRARY_ROOT) -> list[Path]:
    out_dir = library_root / "obsidian" / "Trading Vault" / "03 Strategy Ideas"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for card in cards:
        path = out_dir / strategy_card_filename(card.name)
        path.write_text(render_strategy_card_markdown(card), encoding="utf-8")
        written.append(path)
    index = out_dir / "Strategy Cards Index.md"
    lines = ["# Strategy Cards Index", "", "Research-only strategy cards extracted from the trading book library.", ""]
    for card in sorted(cards, key=lambda c: c.name):
        lines.append(f"- [[{card.name}]] — {card.description}")
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written.append(index)
    return written


def connect(database_url: str):
    import psycopg

    return psycopg.connect(database_url)


def upsert_books_and_chunks(conn, books: Sequence[BookText], reset_chunks: bool = False) -> list[BookText]:
    from psycopg.types.json import Jsonb

    saved: list[BookText] = []
    with conn.cursor() as cur:
        for book in books:
            raw = {
                "extractor": EXTRACTOR_NAME,
                "text_path": str(book.text_path),
                "chunk_count": len(book.chunks),
                "metadata": book.metadata,
            }
            cur.execute(
                """
                insert into knowledge.sources (title, author, source_type, file_path, file_hash, notes, raw)
                values (%s, %s, 'book', %s, %s, %s, %s)
                on conflict (file_hash) do update set
                    title = excluded.title,
                    file_path = excluded.file_path,
                    notes = excluded.notes,
                    raw = knowledge.sources.raw || excluded.raw,
                    updated_at = now()
                returning source_id
                """,
                (
                    book.title,
                    str(book.metadata.get("author") or "") or None,
                    str(book.pdf_path or book.text_path),
                    book.file_hash,
                    "Trading library source ingested for strategy-card research.",
                    Jsonb(raw),
                ),
            )
            source_id = cur.fetchone()[0]
            if reset_chunks:
                cur.execute("delete from knowledge.chunks where source_id = %s", (source_id,))
            for chunk in book.chunks:
                cur.execute(
                    """
                    insert into knowledge.chunks
                        (source_id, chunk_index, chapter, section, page_start, page_end, content, token_count, raw)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (source_id, chunk_index) do update set
                        chapter = excluded.chapter,
                        section = excluded.section,
                        page_start = excluded.page_start,
                        page_end = excluded.page_end,
                        content = excluded.content,
                        token_count = excluded.token_count,
                        raw = excluded.raw
                    """,
                    (
                        source_id,
                        chunk.chunk_index,
                        chunk.chapter,
                        chunk.section,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.content,
                        max(1, len(chunk.content.split())),
                        Jsonb({"extractor": EXTRACTOR_NAME, "text_path": str(book.text_path)}),
                    ),
                )
            saved.append(
                BookText(
                    title=book.title,
                    text_path=book.text_path,
                    pdf_path=book.pdf_path,
                    file_hash=book.file_hash,
                    source_id=source_id,
                    chunks=book.chunks,
                    metadata=book.metadata,
                )
            )
    conn.commit()
    return saved


def find_first_matching_chunk_id(conn, books: Sequence[BookText], card: StrategyCard) -> tuple[int | None, int | None]:
    """Return (source_id, chunk_id) for the first source ref/evidence match."""
    with conn.cursor() as cur:
        for ref in card.source_refs:
            title = ref.rsplit(" p", 1)[0].rsplit(" chunk", 1)[0]
            book = next((b for b in books if b.title == title), None)
            if book is None or book.source_id is None:
                continue
            keyword = card.evidence[0][:80] if card.evidence else ""
            if keyword:
                cur.execute(
                    """
                    select chunk_id from knowledge.chunks
                    where source_id = %s and content ilike %s
                    order by chunk_index limit 1
                    """,
                    (book.source_id, f"%{keyword[:40]}%"),
                )
                row = cur.fetchone()
                if row:
                    return book.source_id, row[0]
            cur.execute("select chunk_id from knowledge.chunks where source_id = %s order by chunk_index limit 1", (book.source_id,))
            row = cur.fetchone()
            if row:
                return book.source_id, row[0]
    return None, None


def upsert_strategy_cards(conn, books: Sequence[BookText], cards: Sequence[StrategyCard]) -> None:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute("delete from knowledge.rules where raw->>'extractor' = %s", (EXTRACTOR_NAME,))
        for card in cards:
            source_id, chunk_id = find_first_matching_chunk_id(conn, books, card)
            raw = asdict(card) | {"extractor": EXTRACTOR_NAME, "strategy_card_id": card.card_id}
            statement = f"{card.name}: {card.description}"
            evidence = "\n".join(card.evidence[:3])
            cur.execute(
                """
                insert into knowledge.rules
                    (source_id, chunk_id, rule_type, statement, evidence, market_regime, timeframe, confidence, status, raw)
                values (%s, %s, 'strategy_card', %s, %s, %s, %s, %s, 'draft', %s)
                """,
                (
                    source_id,
                    chunk_id,
                    statement,
                    evidence,
                    card.market_regime,
                    card.timeframe,
                    card.confidence,
                    Jsonb(raw),
                ),
            )
        for card in cards:
            cur.execute(
                """
                insert into knowledge.playbooks
                    (name, description, universe, timeframe, market_regime, entry_rules, exit_rules, risk_rules, invalidation_rules, status, raw)
                values (%s, %s, 'NIFTY 200 / active watchlist', %s, %s, %s, %s, %s, %s, 'draft', %s)
                on conflict (name) do update set
                    description = excluded.description,
                    timeframe = excluded.timeframe,
                    market_regime = excluded.market_regime,
                    entry_rules = excluded.entry_rules,
                    exit_rules = excluded.exit_rules,
                    risk_rules = excluded.risk_rules,
                    invalidation_rules = excluded.invalidation_rules,
                    raw = excluded.raw,
                    updated_at = now()
                """,
                (
                    card.name,
                    card.description,
                    card.timeframe,
                    card.market_regime,
                    Jsonb(card.entry_rules),
                    Jsonb(card.exit_rules),
                    Jsonb(card.risk_rules),
                    Jsonb(card.invalidation_rules),
                    Jsonb(asdict(card) | {"extractor": EXTRACTOR_NAME, "strategy_card_id": card.card_id}),
                ),
            )
    conn.commit()


def write_manifest(books: Sequence[BookText], cards: Sequence[StrategyCard], written_paths: Sequence[Path], library_root: Path = DEFAULT_LIBRARY_ROOT) -> Path:
    out_dir = library_root / "books" / "metadata"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "strategy_card_ingestion_manifest.json"
    payload = {
        "extractor": EXTRACTOR_NAME,
        "book_count": len(books),
        "chunk_count": sum(len(book.chunks) for book in books),
        "strategy_card_count": len(cards),
        "cards": [asdict(card) for card in cards],
        "written_paths": [str(path) for path in written_paths],
        "books": [
            {
                "title": book.title,
                "text_path": str(book.text_path),
                "pdf_path": str(book.pdf_path) if book.pdf_path else None,
                "file_hash": book.file_hash,
                "source_id": book.source_id,
                "chunk_count": len(book.chunks),
            }
            for book in books
        ],
        "safety": "Research-only. No orders placed, approved, modified, or cancelled.",
    }
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest_path


def run_ingestion(library_root: Path, database_url: str, limit: int | None = None, dry_run: bool = False) -> dict[str, object]:
    books = load_book_texts(library_root, limit=limit)
    book_chunks = {book.title: book.chunks for book in books}
    cards = extract_strategy_cards(book_chunks)
    if dry_run:
        return {
            "book_count": len(books),
            "chunk_count": sum(len(book.chunks) for book in books),
            "strategy_card_count": len(cards),
            "cards": [card.name for card in cards],
            "dry_run": True,
        }
    with connect(database_url) as conn:
        saved_books = upsert_books_and_chunks(conn, books)
        cards = extract_strategy_cards({book.title: book.chunks for book in saved_books})
        written_paths = write_obsidian_cards(cards, library_root)
        upsert_strategy_cards(conn, saved_books, cards)
        manifest_path = write_manifest(saved_books, cards, written_paths, library_root)
    return {
        "book_count": len(saved_books),
        "chunk_count": sum(len(book.chunks) for book in saved_books),
        "strategy_card_count": len(cards),
        "cards": [card.name for card in cards],
        "obsidian_files": [str(path) for path in written_paths],
        "manifest_path": str(manifest_path),
        "dry_run": False,
        "safety": "No orders placed, approved, modified, or cancelled.",
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-root", type=Path, default=DEFAULT_LIBRARY_ROOT)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--limit", type=int, default=None, help="Limit number of book text files, for smoke tests.")
    parser.add_argument("--dry-run", action="store_true", help="Parse/generate cards without writing DB/files.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary only.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_ingestion(args.library_root, args.database_url, limit=args.limit, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("Book strategy-card ingestion complete")
        for key in ["book_count", "chunk_count", "strategy_card_count", "manifest_path", "safety"]:
            if key in result:
                print(f"{key}: {result[key]}")
        print("cards:")
        for card in result.get("cards", []):
            print(f"- {card}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
