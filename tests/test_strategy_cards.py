from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.strategy_cards import (
    CARDS_DIR,
    SKIP_FILES,
    LinkedCard,
    CardSummary,
    link_cards_to_strategies,
    load_strategy_cards,
    parse_strategy_card,
    split_sections,
)

BANKNIFTY_CONFIG = json.loads((PROJECT_ROOT / "config" / "banknifty_options_paper.json").read_text(encoding="utf-8"))
PACK_CONFIG = json.loads((PROJECT_ROOT / "config" / "nse_intraday_options_strategy_pack.json").read_text(encoding="utf-8"))


def test_every_real_card_distills_completely() -> None:
    """Drift alarm: every card in docs/strategy-cards must distill into a
    non-empty what/entry/exit/risk summary (via parsing or an override)."""
    cards = load_strategy_cards()
    expected_count = len([p for p in CARDS_DIR.glob("*.md") if p.name not in SKIP_FILES])
    assert len(cards) == expected_count and expected_count >= 19
    problems: list[str] = []
    for card in cards:
        for field_name in ("what", "entry", "exits", "risk"):
            if not getattr(card, field_name):
                problems.append(f"{card.file_name}: empty {field_name}")
    assert not problems, "cards need parser/override attention:\n" + "\n".join(problems)


def test_split_sections_extracts_title_and_headings() -> None:
    title, sections = split_sections("# My Card\n\n## Status\n- Status: draft\n\n## Entry rules\n- buy the dip\n")
    assert title == "My Card"
    assert set(sections) == {"status", "entry rules"}
    assert "buy the dip" in sections["entry rules"]


def test_parse_strategy_card_maps_dialects() -> None:
    pack_style = (
        "# Pack Card\n\n## Status\n- Card type: entry\n- Implementation status: research-only\n\n"
        "## Core idea\nFade the stretch back to VWAP.\n\n## Entry rules\n- price below band\n\n"
        "## Filters\n- range day only\n\n## Risk\n- max loss 1500\n\n## Exits\n- exit at vwap\n"
    )
    card = parse_strategy_card(pack_style, "pack.md")
    assert card.what == "Fade the stretch back to VWAP."
    assert card.entry == ["price below band"]
    assert card.exits == ["exit at vwap"]
    assert card.risk == ["max loss 1500"]
    assert card.filters == ["range day only"]
    assert card.status["card type"] == "entry"

    book_style = (
        "# Book Card\n\n## Status\n- Status: draft / research-only\n- Confidence: 0.85\n\n"
        "## Description\nMomentum continuation after a breakout holds.\n\n"
        "## Entry rules\n- break and hold\n\n## Exit rules\n- exit at target\n\n"
        "## Invalidation rules\n- stale data\n\n## Risk rules\n- stop below level\n"
    )
    card = parse_strategy_card(book_style, "book.md")
    assert card.what.startswith("Momentum continuation")
    assert card.entry == ["break and hold"]
    assert card.exits == ["exit at target", "stale data"]  # invalidation folds into exits
    assert card.risk == ["stop below level"]
    assert card.status["confidence"] == "0.85"


def test_linker_matches_live_configs_by_name() -> None:
    linked = link_cards_to_strategies(load_strategy_cards(), BANKNIFTY_CONFIG, PACK_CONFIG)
    by_title = {item.card.title: item for item in linked}

    active = by_title["BankNifty Constituent-Led Directional Long Options"]
    assert active.engine == "banknifty_options_paper"
    assert active.strategy_id == "banknifty_constituent_led_directional_long_options"
    assert active.live_status_label == "ACTIVE (paper)"

    pack_card = by_title["Nifty ORB Debit Spread"]
    assert pack_card.engine == "nse_intraday_options_strategy_pack"
    assert pack_card.strategy_id == "nifty_orb_debit_spread"

    blocked = by_title["Options 360 Short Straddle / Strangle Premium Decay"]
    assert blocked.engine == "banknifty_options_paper"
    assert blocked.live_status_label == "disabled"

    research = by_title["Breakout Continuation"]
    assert research.engine is None
    assert research.live_status_label == "research only — not wired to an engine"


def test_all_engine_strategies_have_a_card() -> None:
    """Every strategy wired in a config should have a strategy card to show."""
    linked = link_cards_to_strategies(load_strategy_cards(), BANKNIFTY_CONFIG, PACK_CONFIG)
    wired_ids = {item.strategy_id for item in linked if item.engine is not None}
    router_ids = {str(item.get("id")) for item in BANKNIFTY_CONFIG["strategy_router"]}
    pack_ids = set(PACK_CONFIG["strategies"])
    missing = (router_ids | pack_ids) - wired_ids
    assert not missing, f"strategies without a matching card: {sorted(missing)}"


def test_live_status_label_states() -> None:
    card = CardSummary(file_name="x.md", title="X")
    assert LinkedCard(card=card).live_status_label.startswith("research only")
    assert LinkedCard(card=card, engine="e", enabled=True, paper_trade_enabled=True).live_status_label == "ACTIVE (paper)"
    assert LinkedCard(card=card, engine="e", enabled=True, paper_trade_enabled=False).live_status_label == "enabled (paper entries off)"
    assert LinkedCard(card=card, engine="e", enabled=False, paper_trade_enabled=False).live_status_label == "disabled"
