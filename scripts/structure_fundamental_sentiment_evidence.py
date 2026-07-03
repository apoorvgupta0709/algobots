#!/usr/bin/env python3
"""Structure fundamental/sentiment evidence from stored deep-research runs.

This is read-only research support: it derives auditable evidence snapshots for
scoring and reporting. It does not place, modify, cancel, or recommend orders.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")
MIGRATION = PROJECT_ROOT / "migrations" / "006_symbol_evidence_snapshots.sql"
TWO_PLACES = Decimal("0.01")

POSITIVE_FUNDAMENTAL_TERMS = {
    "revenue growth", "growth", "profit", "margin", "ebitda", "earnings visibility",
    "cash flow", "free cash flow", "roe", "roce", "core infrastructure", "supportive",
    "stable", "improve", "expansion", "order book", "commissioning",
}
NEGATIVE_FUNDAMENTAL_TERMS = {
    "loss", "debt", "leverage", "margin compression", "capex", "governance",
    "probe", "sanctions", "regulatory", "litigation", "pledge", "cash burn",
    "working capital", "risk", "overhang",
}
POSITIVE_SENTIMENT_TERMS = {
    "positive catalyst", "catalyst", "rally", "breakout", "closure", "settlement",
    "approval", "upgrade", "relief", "supportive", "momentum", "52-week high",
}
NEGATIVE_SENTIMENT_TERMS = {
    "negative", "downgrade", "probe", "fraud", "bribery", "sanctions", "event risk",
    "governance scrutiny", "overbought", "volatility", "sell-off", "uncertainty",
}


@dataclass(frozen=True)
class EvidenceSnapshot:
    symbol: str
    evidence_source: str
    source_run_id: int | None
    as_of: datetime
    fundamental_label: str
    fundamental_score: Decimal
    sentiment_label: str
    sentiment_score: Decimal
    confidence: str
    summary: str
    citations: list[dict[str, Any]]
    raw: dict[str, Any]


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def count_terms(text: str, terms: Iterable[str]) -> int:
    lowered = text.lower()
    hits = 0
    for term in terms:
        pattern = r"\b" + re.escape(term.lower()) + r"\b"
        if re.search(pattern, lowered):
            hits += 1
    return hits


def bounded_score(base: Decimal, positive_hits: int, negative_hits: int, max_score: Decimal) -> Decimal:
    score = base + Decimal(positive_hits * 2) - Decimal(negative_hits * 2)
    return q2(max(Decimal("0"), min(max_score, score)))


def label_fundamental(score: Decimal, positive_hits: int, negative_hits: int) -> str:
    if positive_hits == 0 and negative_hits == 0:
        return "insufficient_data"
    if positive_hits >= 3 and negative_hits >= 3:
        return "mixed"
    if score >= Decimal("20"):
        return "strong"
    if score >= Decimal("12"):
        return "acceptable"
    if score > Decimal("0"):
        return "weak"
    return "insufficient_data"


def label_sentiment(score: Decimal, positive_hits: int, negative_hits: int) -> str:
    if positive_hits == 0 and negative_hits == 0:
        return "insufficient_data"
    if negative_hits >= 3 and "event risk":
        # Kept intentionally conservative: regulatory/legal/event-heavy reports should not be treated as clean positive sentiment.
        if positive_hits >= 2:
            return "mixed"
        return "event_risk"
    if positive_hits >= 2 and negative_hits >= 2:
        return "mixed"
    if score >= Decimal("13"):
        return "positive"
    if score >= Decimal("7"):
        return "neutral"
    return "negative"


def classify_deep_research_answer(
    *,
    symbol: str,
    status: str,
    answer: str | None,
    citations: Sequence[dict[str, Any]] | None,
    source_run_id: int | None,
    as_of: datetime | None = None,
) -> EvidenceSnapshot:
    as_of = as_of or datetime.now(timezone.utc)
    text = answer or ""
    clean_citations = list(citations or [])
    if status != "success" or len(text.strip()) < 40:
        return EvidenceSnapshot(
            symbol=symbol,
            evidence_source="deep_research",
            source_run_id=source_run_id,
            as_of=as_of,
            fundamental_label="insufficient_data",
            fundamental_score=Decimal("0"),
            sentiment_label="insufficient_data",
            sentiment_score=Decimal("0"),
            confidence="low",
            summary="Structured evidence unavailable: deep research did not return a usable successful answer.",
            citations=clean_citations,
            raw={"status": status, "answer_chars": len(text)},
        )

    pos_f = count_terms(text, POSITIVE_FUNDAMENTAL_TERMS)
    neg_f = count_terms(text, NEGATIVE_FUNDAMENTAL_TERMS)
    pos_s = count_terms(text, POSITIVE_SENTIMENT_TERMS)
    neg_s = count_terms(text, NEGATIVE_SENTIMENT_TERMS)

    fundamental_score = bounded_score(Decimal("12"), pos_f, neg_f, Decimal("25"))
    sentiment_score = bounded_score(Decimal("10"), pos_s, neg_s, Decimal("20"))
    fundamental_label = label_fundamental(fundamental_score, pos_f, neg_f)
    sentiment_label = label_sentiment(sentiment_score, pos_s, neg_s)
    confidence = "medium" if clean_citations else "low"
    summary = (
        f"Research-derived F/S evidence: fundamental {fundamental_label} "
        f"({fundamental_score}/25), sentiment {sentiment_label} ({sentiment_score}/20). "
        "Heuristic score from cited deep-research text; review source report before acting."
    )
    return EvidenceSnapshot(
        symbol=symbol,
        evidence_source="deep_research",
        source_run_id=source_run_id,
        as_of=as_of,
        fundamental_label=fundamental_label,
        fundamental_score=fundamental_score,
        sentiment_label=sentiment_label,
        sentiment_score=sentiment_score,
        confidence=confidence,
        summary=summary,
        citations=clean_citations,
        raw={
            "status": status,
            "answer_chars": len(text),
            "positive_fundamental_hits": pos_f,
            "negative_fundamental_hits": neg_f,
            "positive_sentiment_hits": pos_s,
            "negative_sentiment_hits": neg_s,
        },
    )


def build_upsert_payload(snapshot: EvidenceSnapshot) -> dict[str, Any]:
    return {
        "symbol": snapshot.symbol,
        "as_of": snapshot.as_of,
        "evidence_source": snapshot.evidence_source,
        "source_run_id": snapshot.source_run_id,
        "fundamental_label": snapshot.fundamental_label,
        "fundamental_score": snapshot.fundamental_score,
        "sentiment_label": snapshot.sentiment_label,
        "sentiment_score": snapshot.sentiment_score,
        "confidence": snapshot.confidence,
        "summary": snapshot.summary,
        "citations": snapshot.citations,
        "raw": snapshot.raw,
    }


def apply_migration(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text())
    conn.commit()


def load_deep_research_runs(conn: psycopg.Connection, run_ids: Sequence[int] | None, symbols: Sequence[str] | None) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if run_ids:
        where.append("deep_research_run_id = any(%s)")
        params.append(list(run_ids))
    if symbols:
        where.append("symbols && %s::text[]")
        params.append(list(symbols))
    clause = " where " + " and ".join(where) if where else ""
    query = f"""
        select deep_research_run_id, symbols, answer, citations, status, created_at
        from research.deep_research_runs
        {clause}
        order by created_at desc
        limit 20
    """
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [
        {
            "deep_research_run_id": int(row[0]),
            "symbols": list(row[1] or []),
            "answer": row[2],
            "citations": row[3] or [],
            "status": row[4],
            "created_at": row[5],
        }
        for row in rows
    ]


def upsert_snapshot(conn: psycopg.Connection, snapshot: EvidenceSnapshot) -> int:
    payload = build_upsert_payload(snapshot)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into research.symbol_evidence_snapshots(
                symbol, as_of, evidence_source, source_run_id, fundamental_label,
                fundamental_score, sentiment_label, sentiment_score, confidence,
                summary, citations, raw
            ) values (
                %(symbol)s, %(as_of)s, %(evidence_source)s, %(source_run_id)s,
                %(fundamental_label)s, %(fundamental_score)s, %(sentiment_label)s,
                %(sentiment_score)s, %(confidence)s, %(summary)s,
                %(citations)s::jsonb, %(raw)s::jsonb
            )
            on conflict(symbol, evidence_source, source_run_id) do update set
                as_of = excluded.as_of,
                fundamental_label = excluded.fundamental_label,
                fundamental_score = excluded.fundamental_score,
                sentiment_label = excluded.sentiment_label,
                sentiment_score = excluded.sentiment_score,
                confidence = excluded.confidence,
                summary = excluded.summary,
                citations = excluded.citations,
                raw = excluded.raw
            returning symbol_evidence_snapshot_id
            """,
            {
                **payload,
                "citations": json.dumps(payload["citations"]),
                "raw": json.dumps(payload["raw"]),
            },
        )
        snapshot_id = int(cur.fetchone()[0])
    conn.commit()
    return snapshot_id


def normalize_symbols(values: Sequence[str] | None) -> list[str]:
    symbols: list[str] = []
    for value in values or []:
        cleaned = value.strip().upper()
        if not cleaned:
            continue
        if ":" not in cleaned and cleaned.replace("-", "").isalnum():
            cleaned = f"NSE:{cleaned}-EQ"
        symbols.append(cleaned)
    return sorted(dict.fromkeys(symbols))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Structure F/S evidence from deep research runs; no orders placed")
    parser.add_argument("--run-ids", nargs="*", type=int, help="Deep research run IDs to structure")
    parser.add_argument("--symbols", nargs="*", help="Optional symbol filter, e.g. ADANIENT or NSE:ADANIENT-EQ")
    parser.add_argument("--apply-migration", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols = normalize_symbols(args.symbols)
    with psycopg.connect(args.database_url) as conn:
        if args.apply_migration:
            apply_migration(conn)
        rows = load_deep_research_runs(conn, args.run_ids, symbols)
        if not rows:
            raise SystemExit("No matching deep research runs found.")
        written = 0
        for row in rows:
            row_symbols = row["symbols"] or symbols
            for symbol in row_symbols:
                snapshot = classify_deep_research_answer(
                    symbol=symbol,
                    status=row["status"],
                    answer=row["answer"],
                    citations=row["citations"],
                    source_run_id=row["deep_research_run_id"],
                    as_of=row["created_at"],
                )
                if args.dry_run:
                    print(json.dumps(build_upsert_payload(snapshot), default=str))
                else:
                    snapshot_id = upsert_snapshot(conn, snapshot)
                    written += 1
                    print(
                        f"Stored evidence snapshot {snapshot_id}: {snapshot.symbol} "
                        f"F={snapshot.fundamental_label} {snapshot.fundamental_score}/25; "
                        f"S={snapshot.sentiment_label} {snapshot.sentiment_score}/20; confidence={snapshot.confidence}"
                    )
    if not args.dry_run:
        print(f"Structured {written} F/S evidence snapshots. Read-only research; no orders placed.")


if __name__ == "__main__":
    main()
