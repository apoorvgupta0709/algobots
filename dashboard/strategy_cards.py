#!/usr/bin/env python3
"""Distill docs/strategy-cards/*.md into uniform summaries for the dashboard.

Pure parsing module: no Streamlit, no DB, no network. The cards come in a few
heading dialects (book cards, strategy-pack cards, filter cards); this maps
them all onto one simple shape — what it does / when it enters / when it exits
/ risk rules / status — with config/strategy_card_overrides.json as the escape
hatch for cards whose headings don't fit the candidate lists.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CARDS_DIR = PROJECT_ROOT / "docs" / "strategy-cards"
OVERRIDES_PATH = PROJECT_ROOT / "config" / "strategy_card_overrides.json"
SKIP_FILES = {"README.md", "Strategy Cards Index.md"}

# Candidate headings per distilled field, lowercase. Exact match wins; a
# trailing-* entry matches any heading starting with that prefix.
WHAT_HEADINGS = ("core idea", "description", "entry thesis")
ENTRY_HEADINGS = ("entry rules", "setup rules", "setup *")
EXIT_HEADINGS = (
    "exits",
    "exit rules",
    "stop-loss rules",
    "target and trailing rules",
    "invalidation rules",
    "invalidations / no-trade filters",
    "adjustment *",
)
RISK_HEADINGS = ("risk", "risk rules", "risk controls", "structure and sizing", "risk rules *", "position sizing")
FILTER_HEADINGS = ("filters", "no-trade filters")

MAX_BULLETS_PER_FIELD = 5


@dataclass
class CardSummary:
    file_name: str
    title: str
    what: str = ""
    entry: list[str] = field(default_factory=list)
    exits: list[str] = field(default_factory=list)
    risk: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)
    full_markdown: str = ""


@dataclass
class LinkedCard:
    card: CardSummary
    engine: str | None = None
    strategy_id: str | None = None
    enabled: bool | None = None
    paper_trade_enabled: bool | None = None

    @property
    def live_status_label(self) -> str:
        if self.engine is None:
            return "research only — not wired to an engine"
        if self.enabled and self.paper_trade_enabled:
            return "ACTIVE (paper)"
        if self.enabled:
            return "enabled (paper entries off)"
        return "disabled"


def split_sections(text: str) -> tuple[str, dict[str, str]]:
    """Return (title, {lowercase heading: body}) split on '## ' headings."""
    title = ""
    sections: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(body).strip()
            current = line[3:].strip().lower()
            body = []
            continue
        if current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body).strip()
    return title, sections


def find_sections(sections: dict[str, str], candidates: tuple[str, ...]) -> list[str]:
    """Bodies of every section matching the candidate headings, in card order."""
    matched: list[str] = []
    for heading, body in sections.items():
        for candidate in candidates:
            if candidate.endswith("*"):
                if heading.startswith(candidate[:-1].strip()):
                    matched.append(body)
                    break
            elif heading == candidate:
                matched.append(body)
                break
    return matched


def bullets(body: str, limit: int = MAX_BULLETS_PER_FIELD) -> list[str]:
    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            out.append(stripped[2:].strip())
        if len(out) >= limit:
            break
    return out


def collect_bullets(sections: dict[str, str], candidates: tuple[str, ...], limit: int = MAX_BULLETS_PER_FIELD) -> list[str]:
    out: list[str] = []
    for body in find_sections(sections, candidates):
        out.extend(bullets(body, limit=limit - len(out)))
        if len(out) >= limit:
            break
    return out


def first_paragraph(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[2:].strip() if stripped.startswith(("- ", "* ")) else stripped
    return ""


def parse_status_bullets(body: str) -> dict[str, str]:
    status: dict[str, str] = {}
    for item in bullets(body, limit=20):
        if ":" in item:
            key, _, value = item.partition(":")
            status[key.strip().lower()] = value.strip()
    return status


def parse_strategy_card(text: str, file_name: str) -> CardSummary:
    title, sections = split_sections(text)
    what_bodies = find_sections(sections, WHAT_HEADINGS)
    return CardSummary(
        file_name=file_name,
        title=title or Path(file_name).stem,
        what=first_paragraph(what_bodies[0]) if what_bodies else "",
        entry=collect_bullets(sections, ENTRY_HEADINGS),
        exits=collect_bullets(sections, EXIT_HEADINGS),
        risk=collect_bullets(sections, RISK_HEADINGS),
        filters=collect_bullets(sections, FILTER_HEADINGS),
        status=parse_status_bullets(sections.get("status", "")),
        full_markdown=text,
    )


def apply_overrides(card: CardSummary, overrides: dict[str, Any]) -> CardSummary:
    fields = overrides.get(card.file_name)
    if not isinstance(fields, dict):
        return card
    if isinstance(fields.get("what"), str):
        card.what = fields["what"]
    for name in ("entry", "exits", "risk", "filters"):
        value = fields.get(name)
        if isinstance(value, list):
            setattr(card, name, [str(item) for item in value])
    if isinstance(fields.get("status"), dict):
        card.status.update({str(k).lower(): str(v) for k, v in fields["status"].items()})
    return card


def load_strategy_cards(
    cards_dir: Path = CARDS_DIR,
    overrides_path: Path = OVERRIDES_PATH,
) -> list[CardSummary]:
    overrides: dict[str, Any] = {}
    if overrides_path.exists():
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
    cards: list[CardSummary] = []
    for path in sorted(cards_dir.glob("*.md")):
        if path.name in SKIP_FILES:
            continue
        card = parse_strategy_card(path.read_text(encoding="utf-8"), path.name)
        cards.append(apply_overrides(card, overrides))
    return cards


def normalize_title(value: str) -> str:
    """Match card H1 titles to config names despite cosmetic differences
    ('Strategy: ' prefixes, 'A / B' vs 'A/B' slash spacing, double spaces)."""
    text = value.strip().lower()
    if text.startswith("strategy:"):
        text = text[len("strategy:"):].strip()
    text = re.sub(r"\s*/\s*", "/", text)
    return re.sub(r"\s+", " ", text)


def link_cards_to_strategies(
    cards: list[CardSummary],
    banknifty_config: dict[str, Any],
    pack_config: dict[str, Any],
) -> list[LinkedCard]:
    """Attach live enabled/paper flags from the engine configs, matched by name."""
    by_name: dict[str, tuple[str, str, bool, bool]] = {}
    for item in banknifty_config.get("strategy_router") or []:
        if isinstance(item, dict) and item.get("name"):
            by_name[normalize_title(str(item["name"]))] = (
                "banknifty_options_paper",
                str(item.get("id") or item.get("strategy_id") or ""),
                item.get("enabled") is True,
                item.get("paper_trade_enabled") is True,
            )
    for sid, strat in (pack_config.get("strategies") or {}).items():
        if isinstance(strat, dict) and strat.get("name"):
            by_name[normalize_title(str(strat["name"]))] = (
                "nse_intraday_options_strategy_pack",
                str(sid),
                strat.get("enabled") is True,
                strat.get("paper_trade_enabled") is True,
            )
    linked: list[LinkedCard] = []
    for card in cards:
        match = by_name.get(normalize_title(card.title))
        if match is None:
            linked.append(LinkedCard(card=card))
        else:
            engine, strategy_id, enabled, paper_enabled = match
            linked.append(
                LinkedCard(
                    card=card,
                    engine=engine,
                    strategy_id=strategy_id,
                    enabled=enabled,
                    paper_trade_enabled=paper_enabled,
                )
            )
    return linked
