#!/usr/bin/env python3
"""Generate the BankNifty after-market day-pattern report for a session.

Research / paper-only. Reads the persisted features + classification for a date
from PostgreSQL and renders a Markdown + JSON report answering:

    * What kind of day was today?           (classification)
    * What evidence supports it?            (ORB / VWAP / close location / breadth /
                                             realized vol / option IV+OI availability)
    * Which past days were most similar?    (nearest-neighbour library)
    * How could it have been played?        (paper/research wording, runner exits)
    * Bot lessons                           (allowed/blocked entries, no-chase, exits)

The "how it could have been played" and "bot lessons" sections always describe
the runner-style exit model — after +0.5R move the paper stop to breakeven + one
tick / cost proxy, then trail via MFE ratchet / structure trailing — never a
fixed profit cap. No FYERS order APIs are imported or called.

Usage:
    uv run python scripts/generate_banknifty_trend_pattern_report.py --date 2026-06-16 --print
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.banknifty_trend_patterns import (  # noqa: E402
    EXIT_MODEL_SENTENCE,
    IST,
    BankNiftyDayFeatures,
    DaySegment,
    PatternClassification,
    summarize_playbook,
    validate_pattern_config_safety,
)

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "banknifty_trend_patterns.json"
REPORT_DIR = PROJECT_ROOT / "reports" / "banknifty_trend_patterns"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")


def load_report_config(path: Path | str = DEFAULT_CONFIG) -> dict[str, Any]:
    """Load + safety-validate the trend-pattern config for reporting. Rejects any
    config that is not paper-only / runner-style (same rails as the builder)."""
    cfg = json.loads(Path(path).read_text())
    validate_pattern_config_safety(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# Row -> dataclass reconstruction (pure)
# --------------------------------------------------------------------------- #
def _parse_json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def features_from_row(row: Mapping[str, Any]) -> BankNiftyDayFeatures:
    """Reconstruct BankNiftyDayFeatures from a persisted feature row (dict)."""
    segments = [
        DaySegment(
            name=s.get("name", ""), start_ist=s.get("start_ist", ""), end_ist=s.get("end_ist", ""),
            return_pct=_dec(s.get("return_pct")), range_pct=_dec(s.get("range_pct")),
            vwap_side_pct=_dec(s.get("vwap_side_pct")), net_direction=s.get("net_direction", "flat"),
            volume_share=_dec(s.get("volume_share")), close_location=_dec(s.get("close_location")),
            candle_count=int(s.get("candle_count", 0)),
        )
        for s in _parse_json(row.get("segments"), [])
    ]
    return BankNiftyDayFeatures(
        session_date=str(row["session_date"]),
        underlying=row.get("underlying", "BANKNIFTY"),
        underlying_symbol=row.get("underlying_symbol", "NSE:NIFTYBANK-INDEX"),
        resolution=str(row.get("resolution", "5")),
        open=_dec(row.get("open")), high=_dec(row.get("high")), low=_dec(row.get("low")),
        close=_dec(row.get("close")), prev_close=_dec(row.get("prev_close")),
        gap_pct=_dec(row.get("gap_pct")), day_return_pct=_dec(row.get("day_return_pct")),
        day_range_pct=_dec(row.get("day_range_pct")), orb_high=_dec(row.get("orb_high")),
        orb_low=_dec(row.get("orb_low")), orb_range_pct=_dec(row.get("orb_range_pct")),
        orb_break_direction=row.get("orb_break_direction", "none"),
        orb_hold=bool(row.get("orb_hold", False)), close_location=_dec(row.get("close_location")),
        vwap_cross_count=int(row.get("vwap_cross_count", 0)), vwap_side_pct=_dec(row.get("vwap_side_pct")),
        realized_vol=_dec(row.get("realized_vol")), range_vs_adr10=_dec(row.get("range_vs_adr10")),
        mfe_from_open_pct=_dec(row.get("mfe_from_open_pct")), mae_from_open_pct=_dec(row.get("mae_from_open_pct")),
        day_high_time=row.get("day_high_time"), day_low_time=row.get("day_low_time"),
        weighted_positive_breadth_pct=_dec(row.get("weighted_positive_breadth_pct")),
        weighted_negative_breadth_pct=_dec(row.get("weighted_negative_breadth_pct")),
        weighted_vwap_confirm_pct=_dec(row.get("weighted_vwap_confirm_pct")),
        breadth_divergence=bool(row.get("breadth_divergence", False)),
        top_positive_contributors=_parse_json(row.get("top_positive_contributors"), []),
        top_negative_contributors=_parse_json(row.get("top_negative_contributors"), []),
        atm_iv=_dec(row.get("atm_iv")), iv_regime=row.get("iv_regime"), pcr=_dec(row.get("pcr")),
        max_pain_distance_pct=_dec(row.get("max_pain_distance_pct")),
        option_chain_available=bool(row.get("option_chain_available", False)),
        candle_count=int(row.get("candle_count", 0)), segments=segments,
        warnings=_parse_json(row.get("warnings"), []),
    )


def classification_from_row(row: Mapping[str, Any]) -> PatternClassification:
    return PatternClassification(
        session_date=str(row["session_date"]),
        primary_class=row["primary_class"],
        direction=row.get("direction") or "neutral",
        confidence=_dec(row.get("confidence")) or Decimal("0"),
        rule_version=row.get("rule_version", ""),
        algorithm=row.get("algorithm", "deterministic_rules"),
        secondary_tags=list(row.get("secondary_tags") or []),
        explanation=_parse_json(row.get("explanation"), {}),
        similar_days=_parse_json(row.get("similar_days"), []),
    )


# --------------------------------------------------------------------------- #
# Rendering (pure)
# --------------------------------------------------------------------------- #
def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, Decimal):
        return f"{value}{suffix}"
    return f"{value}{suffix}"


def render_report(
    feats: BankNiftyDayFeatures,
    label: PatternClassification,
    config: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Return (markdown, json_payload). Pure — no DB / file IO."""
    playbook = summarize_playbook(feats, label, config)
    underlying = feats.underlying

    lines: list[str] = []
    lines.append(f"# BankNifty Day Pattern — {feats.session_date} ({underlying})")
    lines.append("")
    lines.append("Research / paper-only after-market analysis. No live orders; no order placement code.")
    lines.append("")

    # 1. Classification
    lines.append("## 1. Classification")
    lines.append("")
    lines.append(f"- **Class:** `{label.primary_class}`")
    lines.append(f"- **Direction:** {label.direction}")
    lines.append(f"- **Confidence:** {label.confidence}")
    lines.append(f"- **Rule version:** {label.rule_version} ({label.algorithm})")
    if label.secondary_tags:
        lines.append(f"- **Secondary tags:** {', '.join(label.secondary_tags)}")
    lines.append("")

    # 2. Evidence
    lines.append("## 2. Evidence")
    lines.append("")
    lines.append(f"- **Open/High/Low/Close:** {_fmt(feats.open)} / {_fmt(feats.high)} / {_fmt(feats.low)} / {_fmt(feats.close)}")
    lines.append(f"- **Gap / Day return / Range:** {_fmt(feats.gap_pct, '%')} / {_fmt(feats.day_return_pct, '%')} / {_fmt(feats.day_range_pct, '%')}")
    lines.append(f"- **ORB:** {_fmt(feats.orb_low)}–{_fmt(feats.orb_high)} (range {_fmt(feats.orb_range_pct, '%')}); break {feats.orb_break_direction}, hold={feats.orb_hold}")
    lines.append(f"- **VWAP:** crosses={feats.vwap_cross_count}, side {_fmt(feats.vwap_side_pct, '%')} of candles above")
    lines.append(f"- **Close location:** {_fmt(feats.close_location)} (0=low, 1=high)")
    lines.append(f"- **Realized vol (5m σ):** {_fmt(feats.realized_vol, '%')}; range vs ADR10 {_fmt(feats.range_vs_adr10)}x")
    lines.append(f"- **Breadth:** +{_fmt(feats.weighted_positive_breadth_pct, '%')} / -{_fmt(feats.weighted_negative_breadth_pct, '%')}; VWAP-confirm {_fmt(feats.weighted_vwap_confirm_pct, '%')}; divergence={feats.breadth_divergence}")
    if feats.option_chain_available:
        lines.append(f"- **Option chain:** ATM IV {_fmt(feats.atm_iv)}, regime {feats.iv_regime}, PCR {_fmt(feats.pcr)}, max-pain dist {_fmt(feats.max_pain_distance_pct, '%')}")
    else:
        lines.append("- **Option chain:** unavailable for this session (warned, not guessed — IV/OI/PCR not used).")
    if feats.top_positive_contributors:
        tops = ", ".join(f"{c['symbol']} ({c['move_pct']}%)" for c in feats.top_positive_contributors)
        lines.append(f"- **Top positive contributors:** {tops}")
    if feats.top_negative_contributors:
        tops = ", ".join(f"{c['symbol']} ({c['move_pct']}%)" for c in feats.top_negative_contributors)
        lines.append(f"- **Top negative contributors:** {tops}")
    if feats.warnings:
        lines.append(f"- **Data warnings:** {'; '.join(feats.warnings)}")
    lines.append("")

    # Segments table
    lines.append("### Day segments")
    lines.append("")
    lines.append("| Segment | Return | Range | VWAP side | Net | Close loc |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for s in feats.segments:
        lines.append(
            f"| {s.name} | {_fmt(s.return_pct, '%')} | {_fmt(s.range_pct, '%')} | "
            f"{_fmt(s.vwap_side_pct, '%')} | {s.net_direction} | {_fmt(s.close_location)} |"
        )
    lines.append("")

    # 3. Similar days
    lines.append("## 3. Similar historical days")
    lines.append("")
    if label.similar_days:
        lines.append("| Date | Class | Direction | Similarity | Note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for s in label.similar_days:
            lines.append(
                f"| {s.get('session_date')} | {s.get('primary_class')} | {s.get('direction')} | "
                f"{s.get('similarity')} | {s.get('note')} |"
            )
    else:
        lines.append("No prior similar sessions in the library yet (insufficient history).")
    lines.append("")

    # 4. How it could have been played
    lines.append("## 4. How it could have been played (paper/research)")
    lines.append("")
    for item in playbook["how_it_could_have_been_played"]:
        lines.append(f"- {item}")
    lines.append(f"- {EXIT_MODEL_SENTENCE}")
    lines.append("")

    # 5. Bot lessons
    lines.append("## 5. Bot lessons")
    lines.append("")
    for item in playbook["bot_lessons"]:
        lines.append(f"- {item}")
    lines.append("")

    markdown = "\n".join(lines)
    payload = {
        "session_date": feats.session_date,
        "underlying": underlying,
        "classification": {
            "primary_class": label.primary_class,
            "direction": label.direction,
            "confidence": float(label.confidence),
            "rule_version": label.rule_version,
            "algorithm": label.algorithm,
            "secondary_tags": label.secondary_tags,
        },
        "evidence": feats.to_feature_dict(),
        "segments": [s.to_dict() for s in feats.segments],
        "similar_days": label.similar_days,
        "playbook": playbook,
        "exit_model": EXIT_MODEL_SENTENCE,
        "paper_only": True,
    }
    return markdown, payload


# --------------------------------------------------------------------------- #
# DB access
# --------------------------------------------------------------------------- #
def connect_db():
    import psycopg

    return psycopg.connect(DATABASE_URL)


def fetch_session_rows(conn, session_date: date) -> tuple[dict[str, Any], dict[str, Any]] | None:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from research.banknifty_day_features where session_date = %s", (session_date,))
        frow = cur.fetchone()
        cur.execute("select * from research.banknifty_day_classifications where session_date = %s", (session_date,))
        crow = cur.fetchone()
    if not frow or not crow:
        return None
    return frow, crow


def persist_report(conn, session_date: date, classification_id: int | None, report_path: str, markdown: str) -> None:
    """Persist the latest report for a session (latest-per-session, like the files,
    which overwrite). Upserts on session_date so re-runs refresh rather than pile
    up duplicate rows."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into research.banknifty_day_pattern_reports
                (session_date, classification_id, report_path, markdown)
            values (%s, %s, %s, %s)
            on conflict (session_date) do update set
                classification_id = excluded.classification_id,
                report_path = excluded.report_path,
                markdown = excluded.markdown,
                generated_at = now()
            """,
            (session_date, classification_id, report_path, markdown),
        )
    conn.commit()


def write_files(session_date: str, markdown: str, payload: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"{session_date}_BANKNIFTY.md"
    js_path = REPORT_DIR / f"{session_date}_BANKNIFTY.json"
    md_path.write_text(markdown)
    js_path.write_text(json.dumps(payload, indent=2, default=str))
    return md_path, js_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate BankNifty after-market day-pattern report (research/paper-only).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--date", dest="single", required=True, help="session date YYYY-MM-DD")
    p.add_argument("--print", dest="do_print", action="store_true")
    p.add_argument("--no-persist", action="store_true", help="do not insert a row into banknifty_day_pattern_reports")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_report_config(args.config)
    session_date = date.fromisoformat(args.single)

    conn = connect_db()
    try:
        rows = fetch_session_rows(conn, session_date)
        if rows is None:
            print(f"No persisted features/classification for {session_date}. "
                  f"Run build_banknifty_trend_pattern_library.py --date {session_date} first.",
                  file=sys.stderr)
            return 1
        frow, crow = rows
        feats = features_from_row(frow)
        label = classification_from_row(crow)
        markdown, payload = render_report(feats, label, config)
        md_path, js_path = write_files(feats.session_date, markdown, payload)
        if not args.no_persist:
            persist_report(conn, session_date, crow.get("classification_id"), str(md_path), markdown)
    finally:
        conn.close()

    if args.do_print:
        print(markdown)
    print(f"Markdown: {md_path}")
    print(f"JSON: {js_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
