#!/usr/bin/env python3
"""Upgrade Apoorv's trading knowledge library.

Research-only. This script ingests official/public NSE Markdown extracts into
knowledge.sources/chunks with local embeddings, cleans source metadata, tags
non-trading/supporting references, creates source-backed concepts/rules/playbooks,
and writes roadmap/artifact files into the trading-library vault.

It never touches broker APIs, FYERS order endpoints, or live trading settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ingest_books import IngestionConfig, build_embedder, load_config, vector_literal

DSN = "postgresql://hermes@127.0.0.1:55432/finance_tracker"
ROOT = Path("/opt/data/trading-library")
VAULT = ROOT / "obsidian" / "Trading Vault"
REPORT_DIR = ROOT / "reports"
SPEC_DIR = ROOT / "backtest_specs"
EXTRACTED = ROOT / "books" / "legal_sources" / "firecrawl" / "extracted_text"
INGESTOR = "upgrade_trading_knowledge_library_v1"

@dataclass(frozen=True)
class MdSource:
    path: Path
    title: str
    author: str
    notes: str
    tags: list[str]

NSE_SOURCES = [
    MdSource(
        EXTRACTED / "pdf_bank_nifty_option_strategies_booklet_nse.md",
        "NSE Bank Nifty Option Strategies Booklet",
        "National Stock Exchange of India",
        "Official/public NSE payoff booklet extract. Used for BankNifty payoff-structure selection; short-premium content remains research-only.",
        ["nse", "banknifty", "options", "official_public", "payoff_structures"],
    ),
    MdSource(
        EXTRACTED / "pdf_trading_strategies_for_indian_markets_nse.md",
        "NSE Trading Strategies for Indian Markets",
        "National Stock Exchange of India",
        "Official/public NSE trading-strategy education extract. Used for objective/verifiable setup taxonomy and Indian-market context.",
        ["nse", "indian_markets", "strategy_taxonomy", "official_public"],
    ),
    MdSource(
        EXTRACTED / "pdf_nifty_bank_index.md",
        "NSE Nifty Bank Index Factsheet",
        "National Stock Exchange of India",
        "Official/public Nifty Bank factsheet extract. Used for constituent universe, weights, and index-structure context.",
        ["nse", "banknifty", "index_factsheet", "constituents", "official_public"],
    ),
]

METADATA_FIXES = [
    {
        "where_title": "DjVu Document",
        "where_path_like": "%Option Pricing And Volatility%Natenberg%",
        "title": "Option Volatility and Pricing: Advanced Trading Strategies and Techniques",
        "author": "Sheldon Natenberg",
        "tags": ["options", "volatility", "greeks", "metadata_fixed"],
    },
    {
        "where_title": "Trading in the Zone",
        "where_path_like": "%Trading-In-The-Zone%",
        "title": "Trading in the Zone",
        "author": "Mark Douglas",
        "tags": ["psychology", "risk_acceptance", "metadata_fixed"],
    },
    {
        "where_title": "3) Trading Price Action Trading Ranges AL Brooks",
        "where_path_like": "%Trading Price Action Trading Ranges%",
        "title": "Trading Price Action Trading Ranges",
        "author": "Al Brooks",
        "tags": ["price_action", "ranges", "metadata_fixed"],
    },
]

NON_TRADING_TAGS = [
    ("Introduction to Linear Algebra", ["supporting_technical_reference", "math", "not_trading_strategy_source"]),
    ("_Python_practice_Guide", ["supporting_technical_reference", "python", "not_trading_strategy_source"]),
    ("Dev_ops", ["supporting_technical_reference", "devops", "not_trading_strategy_source"]),
    ("Deep_Learning", ["supporting_technical_reference", "ml", "not_trading_strategy_source"]),
    ("Machine_Learning", ["supporting_technical_reference", "ml", "not_trading_strategy_source"]),
    ("Paper Title (use style: paper title)", ["supporting_technical_reference", "data_mining", "not_trading_strategy_source"]),
    ("heard_on_the_street", ["quant_interview_reference", "not_trading_strategy_source"]),
]

CONCEPTS = [
    ("BankNifty constituent leadership", "Use top-weight BankNifty constituents and index structure as the directional evidence layer before selecting CE/PE exposure.", "NSE Nifty Bank Index Factsheet", "Top constituents by weightage"),
    ("Defined-risk option structure", "Only structures with known maximum loss before entry are eligible for automation; undefined-risk premium selling remains research-only.", "NSE Bank Nifty Option Strategies Booklet", "Profit, when"),
    ("Objective verifiable setup", "A trading strategy should be consistent, objective, quantifiable, and verifiable before paper/live promotion.", "NSE Trading Strategies for Indian Markets", "consistent, objective, quantifiable, and verifiable"),
    ("Continuation setup", "Continuation is one of the basic trade setup families; use it only when follow-through confirms trend control.", "NSE Trading Strategies for Indian Markets", "Continuation"),
    ("Reversal setup", "Reversal setups need explicit invalidation because failed breakouts and traps are common in ranges.", "Trading Price Action Trading Ranges", "reversal"),
    ("Range-bound setup", "Range-bound context should prefer mean reversion or no-trade filters over breakout chasing until a confirmed expansion occurs.", "NSE Trading Strategies for Indian Markets", "Range-bound"),
    ("Breakout setup", "Breakout trades require hold/follow-through confirmation; first breakout failures should be treated as a risk event.", "NSE Trading Strategies for Indian Markets", "Break-out"),
    ("Volatility regime filter", "Long options should account for implied/realized volatility, Greeks, and event-driven premium risk before entry.", "Option Volatility and Pricing", "volatility"),
    ("Risk accepted before entry", "Trade size must be reduced until the rupee risk is acceptable; do not widen technical invalidation to fit comfort.", "Trading in the Zone", "Accepting the Risk"),
    ("Overfitting and randomness guard", "Backtests and small samples must be treated as probabilistic evidence, not proof of edge.", "Fooled by Randomness", "randomness"),
]

RULE_DEFS = [
    {
        "concept": "BankNifty constituent leadership",
        "rule_type": "bot_rule",
        "statement": "BankNifty CE/PE direction is invalid unless index structure and top-constituent evidence agree or a documented override blocks the trade.",
        "regime": "BankNifty intraday directional",
        "timeframe": "5m entry / intraday monitor",
    },
    {
        "concept": "Defined-risk option structure",
        "rule_type": "bot_rule",
        "statement": "Automation may select long CE, long PE, or separately backtested debit spreads only; naked short option structures remain research-only.",
        "regime": "all index-option regimes",
        "timeframe": "every trade idea",
    },
    {
        "concept": "Objective verifiable setup",
        "rule_type": "research_gate",
        "statement": "A strategy card cannot move beyond draft unless setup, entry, stop, target, risk, invalidation, and backtest data requirements are explicit.",
        "regime": "all regimes",
        "timeframe": "pre-backtest review",
    },
    {
        "concept": "Risk accepted before entry",
        "rule_type": "risk_rule",
        "statement": "Net modeled loss including costs/slippage must be <= ₹1,500 per trade; if not, reduce quantity/risk budget or skip.",
        "regime": "all paper strategies",
        "timeframe": "pre-entry",
    },
    {
        "concept": "Volatility regime filter",
        "rule_type": "bot_rule",
        "statement": "Block or downsize long-option entries when IV/premium behavior makes stop distance incompatible with the rupee risk cap.",
        "regime": "high IV / event / expiry gamma",
        "timeframe": "pre-entry and open-trade monitor",
    },
    {
        "concept": "Breakout setup",
        "rule_type": "bot_rule",
        "statement": "Breakout entries require follow-through/hold confirmation; a close back inside structure triggers defensive exit or no-trade classification.",
        "regime": "breakout / ORB / CPR trend",
        "timeframe": "5m confirmation",
    },
    {
        "concept": "Overfitting and randomness guard",
        "rule_type": "research_gate",
        "statement": "No strategy is promoted from paper observation to live-review based on a small lucky sample; require deterministic backtest plus multi-week paper journal.",
        "regime": "research governance",
        "timeframe": "post-backtest and paper review",
    },
]

PLAYBOOKS = [
    {
        "name": "BankNifty Constituent-Led Long Options",
        "description": "Long CE/PE BankNifty paper playbook using index structure plus top-constituent confirmation before selecting a defined-risk option structure.",
        "universe": "BANKNIFTY index options; long CE/PE only until debit spreads pass backtests",
        "timeframe": "5-minute entry check with intraday monitor",
        "market_regime": "directional expansion with constituent confirmation",
        "entry": ["Index structure gives bullish/bearish bias", "Top-weight constituents confirm direction", "Option quote is fresh and liquid", "Risk cap passes including costs"],
        "exit": ["Index swing invalidation", "Option premium max-loss guard", "Trailing runner exit after partial favorable move", "Time/EOD exit"],
        "risk": ["paper_only", "live_orders_enabled=false", "max net loss <= ₹1,500/trade", "daily loss guard <= ₹5,000"],
        "invalidation": ["constituents diverge", "breakout fails", "quote stale", "risk cap fails", "undefined-risk structure requested"],
    },
    {
        "name": "NSE Defined-Risk Payoff Selector",
        "description": "Payoff-structure selector derived from NSE option-strategy education; automation is restricted to long options and tested debit spreads.",
        "universe": "NIFTY/BANKNIFTY options",
        "timeframe": "pre-entry structure selection",
        "market_regime": "all regimes; structure router",
        "entry": ["Choose long CE for confirmed bullish thesis", "Choose long PE for confirmed bearish thesis", "Consider debit spread only when implemented/backtested"],
        "exit": ["Close full structure at stop/target", "No legging out without explicit adjustment card"],
        "risk": ["reject unlimited-risk short options", "max loss known before entry", "include costs/slippage in cap"],
        "invalidation": ["max loss unknown", "margin/gap risk unmodeled", "expiry gamma risk unmodeled"],
    },
    {
        "name": "Long Options Volatility and Greeks Gate",
        "description": "Pre-entry filter for long-option trades using premium behavior, IV/realized-volatility context, and Greeks-sensitive risk sizing.",
        "universe": "NIFTY/BANKNIFTY long options",
        "timeframe": "pre-entry and monitor",
        "market_regime": "high IV, event, expiry, or unstable volatility",
        "entry": ["Premium must permit a valid stop under rupee cap", "Prefer fresh quote and stable spread", "Avoid event IV crush unless explicitly modeled"],
        "exit": ["Exit if premium stop or index invalidation triggers", "Do not average down long options"],
        "risk": ["smaller size in high IV", "skip if spread/slippage dominates risk", "cost-aware loss cap"],
        "invalidation": ["stale quote", "wide spread", "IV/event context not modeled"],
    },
]

STRATEGY_SPECS = [
    {
        "strategy_name": "banknifty_constituent_led_long_options_v1",
        "title": "BankNifty Constituent-Led Long Options v1",
        "hypothesis": "BankNifty directional long CE/PE trades have better expectancy when index structure, top-weight constituent momentum/news, and long-option volatility filters align.",
        "target_universe": "BANKNIFTY weekly index options; paper only",
        "timeframe": "5m entries, intraday exits",
        "expected_edge": "Avoids weak index-only entries by requiring constituent confirmation and cost-aware risk cap.",
        "config": {"paper_only": True, "live_orders_enabled": False, "max_net_loss_per_trade": 1500, "daily_loss_cap": 5000, "allowed_structures": ["long_call", "long_put"]},
    },
    {
        "strategy_name": "nifty_expiry_tuesday_defined_risk_v1",
        "title": "Nifty Tuesday Expiry Defined-Risk Directional v1",
        "hypothesis": "Nifty expiry-day directional trades should only run when gamma/event risk is bounded by a defined-risk long-option or debit-spread structure and deterministic stop logic.",
        "target_universe": "NIFTY Tuesday expiry options; research/paper only",
        "timeframe": "intraday expiry session",
        "expected_edge": "Restricts expiry trades to known-loss structures and blocks undefined-risk premium selling.",
        "config": {"paper_only": True, "live_orders_enabled": False, "max_net_loss_per_trade": 1500, "allowed_structures": ["long_call", "long_put", "debit_spread_after_backtest"]},
    },
    {
        "strategy_name": "long_options_volatility_greeks_gate_v1",
        "title": "Long Options Volatility and Greeks Gate v1",
        "hypothesis": "Long-option entries should be blocked or downsized when IV, spread, or premium stop geometry makes net loss exceed the configured risk cap.",
        "target_universe": "NIFTY/BANKNIFTY options overlays",
        "timeframe": "pre-entry + monitor overlay",
        "expected_edge": "Reduces avoidable losses from high premium, IV crush, stale quotes, and gap/slippage conditions.",
        "config": {"paper_only": True, "live_orders_enabled": False, "max_net_loss_per_trade": 1500, "cost_aware": True},
    },
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_markdown(text: str, target_words: int = 760, overlap_words: int = 100) -> list[dict]:
    page_re = re.compile(r"<!--\s*page\s+(\d+)\s*-->", re.I)
    parts = page_re.split(text)
    pages: list[tuple[int | None, str]] = []
    if parts[0].strip():
        pages.append((None, parts[0].strip()))
    for i in range(1, len(parts), 2):
        page = int(parts[i])
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if body.strip():
            pages.append((page, body.strip()))
    if not pages:
        pages = [(None, text)]
    words: list[tuple[str, int | None]] = []
    for page, body in pages:
        for word in body.split():
            words.append((word, page))
    chunks = []
    start = 0
    idx = 0
    step = max(1, target_words - overlap_words)
    while start < len(words):
        window = words[start : start + target_words]
        content = " ".join(w for w, _ in window).strip()
        if content:
            page_nums = [p for _, p in window if p is not None]
            chunks.append({
                "chunk_index": idx,
                "content": content,
                "page_start": min(page_nums) if page_nums else None,
                "page_end": max(page_nums) if page_nums else None,
                "token_count": len(window),
            })
            idx += 1
        if start + target_words >= len(words):
            break
        start += step
    return chunks


def merge_raw(cur, source_id: int, patch: dict) -> None:
    cur.execute(
        "update knowledge.sources set raw = coalesce(raw, '{}'::jsonb) || %s::jsonb, updated_at = now() where source_id = %s",
        (Jsonb(patch), source_id),
    )


def find_source_id(cur, title_like: str) -> int | None:
    cur.execute("select source_id from knowledge.sources where title ilike %s order by source_id limit 1", (f"%{title_like}%",))
    row = cur.fetchone()
    return row[0] if row else None


def find_chunk(cur, title_like: str, query: str) -> tuple[int | None, str | None, int | None]:
    cur.execute(
        """
        select c.chunk_id, left(regexp_replace(c.content, '\\s+', ' ', 'g'), 260), s.source_id
        from knowledge.chunks c join knowledge.sources s on s.source_id=c.source_id
        where s.title ilike %s and c.tsv @@ websearch_to_tsquery('english', %s)
        order by ts_rank(c.tsv, websearch_to_tsquery('english', %s)) desc, c.chunk_id
        limit 1
        """,
        (f"%{title_like}%", query, query),
    )
    row = cur.fetchone()
    if row:
        return row[0], row[1], row[2]
    cur.execute(
        """
        select c.chunk_id, left(regexp_replace(c.content, '\\s+', ' ', 'g'), 260), s.source_id
        from knowledge.chunks c join knowledge.sources s on s.source_id=c.source_id
        where s.title ilike %s
        order by c.chunk_id limit 1
        """,
        (f"%{title_like}%",),
    )
    row = cur.fetchone()
    return (row[0], row[1], row[2]) if row else (None, None, None)


def ingest_nse_sources(conn) -> dict:
    config_path = Path("/opt/data/finance-db/config/ingest_books.json")
    config = load_config(config_path) if config_path.exists() else IngestionConfig()
    embed = build_embedder(config.embedding_model, config.batch_size)
    summary = {"processed": [], "skipped": [], "failed": []}
    with conn.cursor() as cur:
        cur.execute(
            "insert into knowledge.embedding_runs (model_name, embedding_dim, raw) values (%s,%s,%s) returning run_id",
            (config.embedding_model, config.embedding_dim, Jsonb({"ingestor": INGESTOR, "source_kind": "nse_markdown"})),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    chunks_embedded = 0
    try:
        for src in NSE_SOURCES:
            try:
                text = src.path.read_text(encoding="utf-8")
                file_hash = sha256_text(text)
                abs_path = str(src.path)
                chunks = chunk_markdown(text, config.chunk_target_tokens, config.chunk_overlap_tokens)
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("select source_id, file_hash from knowledge.sources where file_path=%s or file_hash=%s", (abs_path, file_hash))
                        existing = cur.fetchone()
                        if existing and existing[1] == file_hash:
                            source_id = existing[0]
                            cur.execute(
                                "update knowledge.sources set title=%s, author=%s, source_type=%s, notes=%s, raw=coalesce(raw,'{}'::jsonb)||%s::jsonb, updated_at=now() where source_id=%s",
                                (src.title, src.author, "official_public_markdown", src.notes, Jsonb({"tags": src.tags, "ingestor": INGESTOR, "source_pdf": re.search(r"Source PDF: `([^`]+)`", text).group(1) if re.search(r"Source PDF: `([^`]+)`", text) else None}), source_id),
                            )
                            summary["skipped"].append({"title": src.title, "reason": "already_current", "chunks": len(chunks)})
                            continue
                        if existing:
                            cur.execute("delete from knowledge.sources where source_id=%s", (existing[0],))
                        cur.execute(
                            "insert into knowledge.sources (title, author, source_type, file_path, file_hash, notes, raw) values (%s,%s,%s,%s,%s,%s,%s) returning source_id",
                            (src.title, src.author, "official_public_markdown", abs_path, file_hash, src.notes, Jsonb({"tags": src.tags, "ingestor": INGESTOR, "source_pdf": re.search(r"Source PDF: `([^`]+)`", text).group(1) if re.search(r"Source PDF: `([^`]+)`", text) else None})),
                        )
                        source_id = cur.fetchone()[0]
                        vectors = embed([c["content"] for c in chunks])
                        if len(vectors) != len(chunks):
                            raise RuntimeError(f"embedding mismatch for {src.title}")
                        for c, v in zip(chunks, vectors):
                            cur.execute(
                                "insert into knowledge.chunks (source_id, chunk_index, page_start, page_end, content, token_count, embedding, raw) values (%s,%s,%s,%s,%s,%s,%s::vector,%s)",
                                (source_id, c["chunk_index"], c["page_start"], c["page_end"], c["content"], c["token_count"], vector_literal(v), Jsonb({"ingestor": INGESTOR, "chunker": "markdown_page_markers"})),
                            )
                        chunks_embedded += len(chunks)
                        summary["processed"].append({"title": src.title, "chunks": len(chunks)})
            except Exception as exc:  # keep batch summary audit
                summary["failed"].append({"title": src.title, "error": str(exc)})
        with conn.cursor() as cur:
            cur.execute(
                "update knowledge.embedding_runs set sources_processed=%s, chunks_embedded=%s, sources_skipped=%s, status=%s, error_text=%s, raw=coalesce(raw,'{}'::jsonb)||%s::jsonb, finished_at=now() where run_id=%s",
                (len(summary["processed"]), chunks_embedded, len(summary["skipped"]), "success" if not summary["failed"] else "error", "; ".join(f"{x['title']}: {x['error']}" for x in summary["failed"]) or None, Jsonb(summary), run_id),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("update knowledge.embedding_runs set status='error', error_text=%s, finished_at=now() where run_id=%s", (str(exc), run_id))
        conn.commit()
        raise
    return summary


def update_metadata(conn) -> dict:
    out = {"metadata_fixed": 0, "tagged_non_trading": 0}
    with conn.transaction(), conn.cursor() as cur:
        for fix in METADATA_FIXES:
            cur.execute(
                """
                update knowledge.sources
                set title=%s, author=%s,
                    raw=coalesce(raw,'{}'::jsonb)||%s::jsonb,
                    updated_at=now()
                where title=%s or file_path ilike %s
                returning source_id
                """,
                (fix["title"], fix["author"], Jsonb({"tags": fix["tags"], "metadata_reviewed_at": datetime.now(timezone.utc).isoformat()}), fix["where_title"], fix["where_path_like"]),
            )
            out["metadata_fixed"] += len(cur.fetchall())
        for title_like, tags in NON_TRADING_TAGS:
            cur.execute(
                """
                update knowledge.sources
                set raw=coalesce(raw,'{}'::jsonb)||%s::jsonb,
                    notes=coalesce(nullif(notes,''),'Tagged during source triage; retained for support/reference, not a primary trading-strategy source.'),
                    updated_at=now()
                where title ilike %s or file_path ilike %s
                returning source_id
                """,
                (Jsonb({"tags": tags, "strategy_source_priority": "supporting_or_non_trading", "triaged_at": datetime.now(timezone.utc).isoformat()}), f"%{title_like}%", f"%{title_like}%"),
            )
            out["tagged_non_trading"] += len(cur.fetchall())
    return out


def upsert_research_objects(conn) -> dict:
    out = {"concepts": 0, "rules": 0, "playbooks": 0, "hypotheses": 0, "strategy_versions": 0, "promoted_existing_playbooks": 0}
    rule_ids_by_name: dict[str, int] = {}
    concept_ids: dict[str, int] = {}
    with conn.transaction(), conn.cursor() as cur:
        for name, desc, title_like, query in CONCEPTS:
            chunk_id, excerpt, source_id = find_chunk(cur, title_like, query)
            cur.execute(
                """
                insert into knowledge.concepts (name, description, source_chunk_id, confidence, status, raw)
                values (%s,%s,%s,%s,'reviewed',%s)
                on conflict (name) do update
                set description=excluded.description, source_chunk_id=excluded.source_chunk_id,
                    confidence=excluded.confidence, status='reviewed', raw=knowledge.concepts.raw || excluded.raw,
                    updated_at=now()
                returning concept_id
                """,
                (name, desc, chunk_id, 0.82, Jsonb({"source_title_like": title_like, "evidence_query": query, "evidence_excerpt": excerpt, "ingestor": INGESTOR})),
            )
            concept_ids[name] = cur.fetchone()[0]
            out["concepts"] += 1
        for rd in RULE_DEFS:
            concept_id = concept_ids.get(rd["concept"])
            cur.execute("select source_chunk_id from knowledge.concepts where concept_id=%s", (concept_id,))
            chunk_id = cur.fetchone()[0] if concept_id else None
            source_id = None
            evidence = None
            if chunk_id:
                cur.execute("select source_id, left(regexp_replace(content,'\\s+',' ','g'),320) from knowledge.chunks where chunk_id=%s", (chunk_id,))
                row = cur.fetchone()
                if row:
                    source_id, evidence = row[0], row[1]
            cur.execute("select rule_id from knowledge.rules where statement=%s limit 1", (rd["statement"],))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "update knowledge.rules set source_id=%s, chunk_id=%s, concept_id=%s, rule_type=%s, evidence=%s, market_regime=%s, timeframe=%s, confidence=%s, status='reviewed', raw=coalesce(raw,'{}'::jsonb)||%s::jsonb, updated_at=now() where rule_id=%s returning rule_id",
                    (source_id, chunk_id, concept_id, rd["rule_type"], evidence, rd["regime"], rd["timeframe"], 0.84, Jsonb({"ingestor": INGESTOR, "paper_only": True}), existing[0]),
                )
            else:
                cur.execute(
                    "insert into knowledge.rules (source_id, chunk_id, concept_id, rule_type, statement, evidence, market_regime, timeframe, confidence, status, raw) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'reviewed',%s) returning rule_id",
                    (source_id, chunk_id, concept_id, rd["rule_type"], rd["statement"], evidence, rd["regime"], rd["timeframe"], 0.84, Jsonb({"ingestor": INGESTOR, "paper_only": True})),
                )
            rid = cur.fetchone()[0]
            rule_ids_by_name[rd["concept"]] = rid
            out["rules"] += 1
        # Promote existing generic draft playbooks to reviewed if they are governance/filter cards, not live approval.
        cur.execute(
            "update knowledge.playbooks set status='reviewed', raw=coalesce(raw,'{}'::jsonb)||%s::jsonb, updated_at=now() where name in ('Breakout Continuation','Failed Breakout Reversal','Trend Pullback Entry','Volatility Regime Filter','Randomness / Overfitting Risk Filter','Position Sizing / Risk Rule Card') returning playbook_id",
            (Jsonb({"review_note": "Reviewed as source-backed research/playbook draft; not live-approved.", "reviewed_by": INGESTOR}),),
        )
        out["promoted_existing_playbooks"] = len(cur.fetchall())
        all_rule_ids = list(rule_ids_by_name.values())
        for pb in PLAYBOOKS:
            cur.execute(
                """
                insert into knowledge.playbooks (name, description, rule_ids, universe, timeframe, market_regime, entry_rules, exit_rules, risk_rules, invalidation_rules, status, raw)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reviewed',%s)
                on conflict (name) do update
                set description=excluded.description, rule_ids=excluded.rule_ids, universe=excluded.universe,
                    timeframe=excluded.timeframe, market_regime=excluded.market_regime,
                    entry_rules=excluded.entry_rules, exit_rules=excluded.exit_rules,
                    risk_rules=excluded.risk_rules, invalidation_rules=excluded.invalidation_rules,
                    status='reviewed', raw=knowledge.playbooks.raw || excluded.raw, updated_at=now()
                returning playbook_id
                """,
                (pb["name"], pb["description"], all_rule_ids, pb["universe"], pb["timeframe"], pb["market_regime"], Jsonb(pb["entry"]), Jsonb(pb["exit"]), Jsonb(pb["risk"]), Jsonb(pb["invalidation"]), Jsonb({"ingestor": INGESTOR, "paper_only": True, "not_live_approved": True})),
            )
            cur.fetchone()
            out["playbooks"] += 1
        for spec in STRATEGY_SPECS:
            source_rule_ids = all_rule_ids
            cur.execute("select hypothesis_id from research.hypotheses where title=%s order by hypothesis_id limit 1", (spec["title"],))
            row = cur.fetchone()
            if row:
                hyp_id = row[0]
                cur.execute(
                    "update research.hypotheses set hypothesis=%s, source_rule_ids=%s, target_universe=%s, timeframe=%s, expected_edge=%s, assumptions=%s, status='ready_for_backtest', updated_at=now() where hypothesis_id=%s",
                    (spec["hypothesis"], source_rule_ids, spec["target_universe"], spec["timeframe"], spec["expected_edge"], Jsonb({"paper_only": True, "source": INGESTOR}), hyp_id),
                )
            else:
                cur.execute(
                    "insert into research.hypotheses (title, hypothesis, source_rule_ids, target_universe, timeframe, expected_edge, assumptions, status) values (%s,%s,%s,%s,%s,%s,%s,'ready_for_backtest') returning hypothesis_id",
                    (spec["title"], spec["hypothesis"], source_rule_ids, spec["target_universe"], spec["timeframe"], spec["expected_edge"], Jsonb({"paper_only": True, "source": INGESTOR})),
                )
                hyp_id = cur.fetchone()[0]
                out["hypotheses"] += 1
            cur.execute(
                """
                insert into research.strategy_versions (hypothesis_id, strategy_name, version, code_path, config, parameters, assumptions, status)
                values (%s,%s,'v1',%s,%s,%s,%s,'draft')
                on conflict (strategy_name, version) do update
                set hypothesis_id=excluded.hypothesis_id, config=excluded.config,
                    parameters=excluded.parameters, assumptions=excluded.assumptions, status='draft'
                returning strategy_version_id
                """,
                (hyp_id, spec["strategy_name"], None, Jsonb(spec["config"]), Jsonb({"requires_deterministic_backtest": True}), "Research-only specification; not yet implemented as deterministic trading code."),
            )
            cur.fetchone()
            out["strategy_versions"] += 1
    return out


def write_artifacts(summary: dict) -> list[str]:
    created: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for d in [VAULT / "05 Roadmaps", VAULT / "02 Source Notes", VAULT / "03 Strategy Ideas", VAULT / "04 Bot Rules", VAULT / "07 Reports", REPORT_DIR, SPEC_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    roadmap = f"""# Trading Knowledge Library Roadmap — 6 Deliverables

Generated: {now}

Status: research/paper-only. This roadmap does **not** approve live trading.

## 1. Better BankNifty/Nifty strategy cards
- Convert source-backed ideas into deterministic cards: setup, entry, stop, target, invalidation, risk, data needs, and source citations.
- Immediate cards: BankNifty constituent-led long options, NSE defined-risk payoff selector, long-options volatility/Greeks gate.
- Promotion gate: source reviewed → deterministic rule → backtest → paper journal → live-review only with explicit approval.

## 2. Bot rules derived from books/sources
- Encode rules as auditable `knowledge.rules` and config/spec checklists.
- Mandatory rules: paper-only, no undefined-risk structures, cost-aware ₹1,500 net trade-loss cap, fresh quotes, index+constituent confirmation.
- Use rules as pre-entry filters before strategy-specific logic.

## 3. Strategy research reports
- Produce source-backed reports for each theme: payoff selection, volatility/Greeks risk, price-action confirmation, position sizing, overfitting guard.
- Reports should cite `knowledge.sources/chunks` and distinguish official NSE sources from general trading books.

## 4. Backtest-ready strategy specs
- Store JSON specs under `backtest_specs/` and DB `research.strategy_versions`.
- Specs are not trading code; they define hypotheses, required data, parameters, costs/slippage, and validation metrics.
- First specs: BankNifty constituent-led long options, Nifty Tuesday expiry defined-risk, long-options volatility/Greeks gate.

## 5. Obsidian notes / research vault
- Keep source notes, strategy cards, bot rules, and roadmap notes in the Obsidian vault.
- Each card includes a processing checklist: reviewed, hypothesis, backtest, paper journal, promotion decision.

## 6. Vector DB improvements
- Ingest official/public NSE Markdown extracts into pgvector.
- Fix bad metadata (Natenberg, Mark Douglas, Al Brooks range book).
- Tag non-trading/supporting PDFs instead of deleting raw data.
- Populate concepts/rules/playbooks and improve BankNifty/NSE search quality.

## Current implementation summary
```json
{json.dumps(summary, indent=2, ensure_ascii=False)}
```
"""
    path = VAULT / "05 Roadmaps" / "Trading Knowledge Library Roadmap - 6 Deliverables.md"
    path.write_text(roadmap, encoding="utf-8")
    created.append(str(path))

    source_note = """# NSE Official/Public Sources in Vector DB

Status: ingested into `knowledge.sources/chunks` with local BGE embeddings.

## Sources
- NSE Bank Nifty Option Strategies Booklet
- NSE Trading Strategies for Indian Markets
- NSE Nifty Bank Index Factsheet

## Use
- Payoff structure selection and defined-risk constraints.
- BankNifty constituent context and index methodology.
- Objective/verifiable setup taxonomy: continuation, reversal, range-bound, breakout.

## Safety
- Short-premium/undefined-risk structures in NSE educational material are research-only.
- Current automation remains long CE/PE only unless debit spreads pass deterministic backtests.
"""
    path = VAULT / "02 Source Notes" / "NSE Official Public Sources in Vector DB.md"
    path.write_text(source_note, encoding="utf-8")
    created.append(str(path))

    bot_rules = """# Source-Backed Bot Rule Pack

Status: reviewed / paper-only. No live execution approval.

## Mandatory pre-entry rules
1. `paper_only=true` and `live_orders_enabled=false` must remain true/false respectively.
2. Net modeled loss including costs/slippage must be <= ₹1,500 per trade.
3. Daily strategy loss guard remains <= ₹5,000.
4. BankNifty direction needs index structure plus constituent confirmation.
5. Only long CE/PE are automation-eligible now; debit spreads need separate backtest; naked short options are blocked.
6. Fresh quote/liquidity check required before selecting an option contract.
7. Breakout entries require hold/follow-through confirmation; close back inside structure is defensive-exit/no-trade evidence.
8. No promotion from paper to live-review without deterministic backtest + multi-week paper journal.

## Implementation note
These rules are also stored in `knowledge.rules` for search/audit. They are specifications, not live order instructions.
"""
    path = VAULT / "04 Bot Rules" / "Source-Backed Bot Rule Pack.md"
    path.write_text(bot_rules, encoding="utf-8")
    created.append(str(path))

    for pb in PLAYBOOKS:
        md = f"""# {pb['name']}

## Status
- Status: reviewed / research-paper only
- Live trading: not approved
- Universe: {pb['universe']}
- Timeframe: {pb['timeframe']}
- Market regime: {pb['market_regime']}

## Description
{pb['description']}

## Entry rules
""" + "\n".join(f"- {x}" for x in pb["entry"]) + "\n\n## Exit rules\n" + "\n".join(f"- {x}" for x in pb["exit"]) + "\n\n## Risk rules\n" + "\n".join(f"- {x}" for x in pb["risk"]) + "\n\n## Invalidation rules\n" + "\n".join(f"- {x}" for x in pb["invalidation"]) + "\n\n## Source links\n- [[NSE Official Public Sources in Vector DB]]\n- [[Source-Backed Bot Rule Pack]]\n\n## Processing checklist\n- [x] Source-backed draft created\n- [ ] Deterministic backtest implemented\n- [ ] Paper-trading journal reviewed\n- [ ] Promotion decision recorded\n"
        safe = re.sub(r"[^A-Za-z0-9 _+-]", "", pb["name"]).strip().replace("  ", " ")
        path = VAULT / "03 Strategy Ideas" / f"{safe}.md"
        path.write_text(md, encoding="utf-8")
        created.append(str(path))

    report = f"""# Trading Knowledge DB Upgrade Report

Generated: {now}

## Summary
- Ingested/updated official-public NSE Markdown extracts.
- Fixed bad source metadata for Natenberg, Mark Douglas, and Al Brooks range book where present.
- Tagged supporting/non-trading PDFs instead of deleting them.
- Populated reviewed concepts, rules, playbooks, hypotheses, and strategy specs.
- Wrote roadmap, source notes, bot rules, strategy cards, and JSON specs.

## DB operation summary
```json
{json.dumps(summary, indent=2, ensure_ascii=False)}
```

## Safety
No broker APIs, no FYERS orders, no live-order config changes.
"""
    path = REPORT_DIR / "trading_knowledge_db_upgrade_report.md"
    path.write_text(report, encoding="utf-8")
    created.append(str(path))
    path2 = VAULT / "07 Reports" / "Trading Knowledge DB Upgrade Report.md"
    path2.write_text(report, encoding="utf-8")
    created.append(str(path2))

    for spec in STRATEGY_SPECS:
        out = {
            **spec,
            "status": "research_spec_ready_for_backtest",
            "safety": {"paper_only": True, "live_orders_enabled": False, "requires_explicit_live_order_confirmation": True},
            "validation_plan": ["compile deterministic strategy", "unit-test entry/exit/risk gates", "proxy backtest", "paper run", "manual review"],
        }
        path = SPEC_DIR / f"{spec['strategy_name']}.json"
        path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        created.append(str(path))
    return created


def verify(conn) -> dict:
    out = {}
    with conn.cursor() as cur:
        cur.execute("select count(*) from knowledge.sources")
        out["sources"] = cur.fetchone()[0]
        cur.execute("select count(*), count(embedding), count(*)-count(embedding) from knowledge.chunks")
        out["chunks"], out["embedded_chunks"], out["missing_embeddings"] = cur.fetchone()
        cur.execute("select count(*) from knowledge.concepts")
        out["concepts"] = cur.fetchone()[0]
        cur.execute("select count(*) from knowledge.rules where status='reviewed'")
        out["reviewed_rules"] = cur.fetchone()[0]
        cur.execute("select count(*) from knowledge.playbooks where status='reviewed'")
        out["reviewed_playbooks"] = cur.fetchone()[0]
        cur.execute("select title, count(c.chunk_id) from knowledge.sources s left join knowledge.chunks c using(source_id) where s.title ilike 'NSE %' group by title order by title")
        out["nse_sources"] = [{"title": r[0], "chunks": r[1]} for r in cur.fetchall()]
        q = "bank nifty option strategy"
        cur.execute(
            """
            select s.title, c.page_start, left(regexp_replace(c.content,'\\s+',' ','g'),220) as excerpt
            from knowledge.chunks c join knowledge.sources s using(source_id)
            where c.tsv @@ websearch_to_tsquery('english', %s)
            order by case when s.title ilike 'NSE%%' then 0 else 1 end, ts_rank(c.tsv, websearch_to_tsquery('english', %s)) desc
            limit 5
            """,
            (q, q),
        )
        out["sample_banknifty_hits"] = [{"title": r[0], "page_start": r[1], "excerpt": r[2]} for r in cur.fetchall()]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-ingest", action="store_true")
    args = parser.parse_args()
    summary = {}
    with psycopg.connect(DSN) as conn:
        summary["ingest"] = {"skipped_by_arg": True} if args.skip_ingest else ingest_nse_sources(conn)
        summary["metadata"] = update_metadata(conn)
        summary["research_objects"] = upsert_research_objects(conn)
        summary["verification"] = verify(conn)
    summary["artifacts"] = write_artifacts(summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
