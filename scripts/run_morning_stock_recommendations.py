#!/usr/bin/env python3
"""Generate 11 AM read-only stock buy-candidate recommendations.

Phase 1: fundamental + sentiment context is attached through cached/live Sonar
Deep Research; technical/risk scoring comes from local FYERS/Postgres factors.
No orders are placed, modified, or cancelled by this script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Sequence

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import generate_daily_market_report as daily
from scripts import run_deep_research_context as deep_research
from scripts import watchlist_utils

DEFAULT_WATCHLIST = PROJECT_ROOT / "watchlists" / "active.csv"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports"
DEFAULT_DATABASE_URL = "postgresql://" + "hermes" + "@" + "127.0.0.1" + ":" + "55432" + "/" + "finance_tracker"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    score: Decimal
    label: str
    technical_score: Decimal
    fundamental_score: Decimal
    sentiment_score: Decimal
    risk_score: Decimal
    entry_condition: str
    stop_loss: Decimal | None
    target: Decimal | None
    max_risk_note: str
    reasons: list[str]
    risks: list[str]
    local_context: dict[str, str]
    deep_research_run_id: int | None = None


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def add_score(current: Decimal, amount: str, reason: str, reasons: list[str]) -> Decimal:
    reasons.append(reason)
    return current + Decimal(amount)


def score_candidate(row: daily.ReportRow, generated_at: datetime | None = None) -> Candidate:
    generated_at = generated_at or datetime.now(timezone.utc)
    reasons: list[str] = []
    risks: list[str] = []
    technical = Decimal("0")
    risk = Decimal("15")
    fundamental = Decimal("0")  # Phase 1: populated as context via deep research, not numeric fundamentals yet.
    sentiment = Decimal("0")  # Phase 1: populated as context via deep research, not numeric sentiment yet.

    freshness = daily.freshness_flags(row, generated_at)
    for flag in freshness:
        risks.append(f"Stale/missing data review: {flag}")
    if freshness:
        risk -= Decimal("8")

    if row.ltp is None or row.candle_close is None:
        risks.append("Missing latest price or candle close")
        return Candidate(
            symbol=row.symbol,
            score=Decimal("0"),
            label="needs_review",
            technical_score=Decimal("0"),
            fundamental_score=fundamental,
            sentiment_score=sentiment,
            risk_score=Decimal("0"),
            entry_condition="Do not act: missing local market data.",
            stop_loss=None,
            target=None,
            max_risk_note="No sizing without current price and stop.",
            reasons=reasons,
            risks=risks,
            local_context=local_context_for_row(row),
        )

    if row.trend == "bullish":
        technical = add_score(technical, "18", "Bullish daily trend: close above key moving averages", reasons)
    elif row.trend == "neutral":
        technical = add_score(technical, "6", "Neutral trend; needs confirmation", reasons)
    else:
        risks.append("Bearish or weak trend structure")

    if row.sma_20 is not None and row.candle_close > row.sma_20:
        technical = add_score(technical, "8", "Close above SMA20", reasons)
    if row.sma_50 is not None and row.candle_close > row.sma_50:
        technical = add_score(technical, "8", "Close above SMA50", reasons)
    if row.sma_200 is not None and row.candle_close > row.sma_200:
        technical = add_score(technical, "6", "Close above SMA200", reasons)

    if row.rsi_14 is not None:
        if Decimal("45") <= row.rsi_14 <= Decimal("68"):
            technical = add_score(technical, "10", f"RSI in constructive range: {daily.format_decimal(row.rsi_14)}", reasons)
        elif row.rsi_14 > Decimal("75"):
            risks.append(f"Momentum may be extended: RSI {daily.format_decimal(row.rsi_14)}")
            technical += Decimal("2")
        elif row.rsi_14 < Decimal("40"):
            risks.append(f"Weak momentum: RSI {daily.format_decimal(row.rsi_14)}")

    if row.macd_histogram is not None and row.macd_histogram > 0:
        technical = add_score(technical, "8", "MACD histogram positive", reasons)
    elif row.macd_histogram is not None and row.macd_histogram < 0:
        risks.append("MACD histogram negative")

    if row.roc_20 is not None and row.roc_20 > 0:
        technical = add_score(technical, "5", f"Positive ROC20: {daily.format_pct(row.roc_20)}", reasons)
    if row.breakout_20 == "yes":
        technical = add_score(technical, "10", "20-day breakout/reclaim setup", reasons)
    if row.breakout_55 == "yes":
        technical = add_score(technical, "6", "55-day breakout/reclaim setup", reasons)

    if row.relative_volume_20 is not None:
        if row.relative_volume_20 >= Decimal("1.5"):
            technical = add_score(technical, "10", f"Volume expansion: RelVol20 {daily.format_decimal(row.relative_volume_20)}", reasons)
        elif row.relative_volume_20 < Decimal("0.5"):
            risks.append(f"Low participation: RelVol20 {daily.format_decimal(row.relative_volume_20)}")
            risk -= Decimal("4")

    if row.atr_pct_14 is not None:
        if Decimal("0.012") <= row.atr_pct_14 <= Decimal("0.045"):
            risk += Decimal("5")
            reasons.append(f"ATR is tradable but not extreme: {daily.format_pct(row.atr_pct_14)}")
        elif row.atr_pct_14 > Decimal("0.06"):
            risks.append(f"Very high volatility: ATR% {daily.format_pct(row.atr_pct_14)}")
            risk -= Decimal("8")
        elif row.atr_pct_14 < Decimal("0.008"):
            risks.append(f"Low movement potential: ATR% {daily.format_pct(row.atr_pct_14)}")
            risk -= Decimal("4")

    risk = max(Decimal("0"), min(Decimal("20"), risk))
    technical = max(Decimal("0"), min(Decimal("60"), technical))
    score = q2(technical + risk + fundamental + sentiment)

    if freshness:
        label = "needs_review"
    elif score >= Decimal("80"):
        label = "buy_candidate_research"
    elif score >= Decimal("70"):
        label = "paper_setup"
    elif score >= Decimal("55"):
        label = "watch"
    else:
        label = "reject"

    stop_loss = row.previous_day_low or row.donchian_20_low
    target = None
    invalid_stop = False
    if stop_loss is not None and row.ltp is not None and row.ltp > stop_loss:
        risk_per_share = row.ltp - stop_loss
        target = row.ltp + (risk_per_share * Decimal("2"))
    elif stop_loss is not None and row.ltp is not None:
        invalid_stop = True
        risks.append("Stop reference is not below LTP; setup needs manual review before any buy-candidate label")
        label = "needs_review"
    entry_condition = f"Only consider if price holds above ₹{row.ltp.quantize(TWO_PLACES)} after 11:00 IST and setup remains valid; no automatic order."
    max_risk_note = "Phase 1 recommendation only. For paper/live sizing later, cap rupee risk per trade before entry."

    return Candidate(
        symbol=row.symbol,
        score=score,
        label=label,
        technical_score=q2(technical),
        fundamental_score=fundamental,
        sentiment_score=sentiment,
        risk_score=q2(risk),
        entry_condition=entry_condition,
        stop_loss=stop_loss,
        target=target,
        max_risk_note=max_risk_note,
        reasons=reasons or ["No strong positive technical evidence"],
        risks=risks,
        local_context=local_context_for_row(row),
    )


def local_context_for_row(row: daily.ReportRow) -> dict[str, str]:
    change, pct = daily.quote_change(row)
    return {
        "ltp": daily.format_inr(row.ltp),
        "change": f"{daily.format_inr(change)} ({daily.format_pct(pct)})",
        "trend": row.trend or "n/a",
        "rsi_14": daily.format_decimal(row.rsi_14),
        "atr_pct_14": daily.format_pct(row.atr_pct_14),
        "relative_volume_20": daily.format_decimal(row.relative_volume_20),
        "breakout_20": row.breakout_20 or "n/a",
        "breakout_55": row.breakout_55 or "n/a",
        "factor_time": daily.format_dt(row.factor_ts),
        "quote_time": daily.format_dt(daily.quote_freshness_time(row)),
    }


def select_candidates(rows: Sequence[daily.ReportRow], generated_at: datetime | None, limit: int) -> list[Candidate]:
    candidates = [score_candidate(row, generated_at) for row in rows]
    candidates.sort(key=lambda item: (item.label == "buy_candidate_research", item.score), reverse=True)
    return candidates[:limit]


def render_money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"₹{value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def render_recommendation_report(
    candidates: Sequence[Candidate],
    generated_at: datetime | None = None,
    deep_research_notes: dict[str, str] | None = None,
) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    deep_research_notes = deep_research_notes or {}
    lines = [
        "## Morning Stock Recommendations",
        f"Generated: {daily.format_dt(generated_at)}",
        "Scope: research-only buy candidates for review after 11:00 IST. Not investment advice. No orders placed.",
        "Phase: 1 — advisory/reporting only; live execution disabled.",
        "",
    ]
    if not candidates:
        lines.extend([
            "## Candidates",
            "No candidates found from current local factor snapshots.",
        ])
        return "\n".join(lines)

    lines.append("## Ranked candidates")
    for idx, candidate in enumerate(candidates, 1):
        lines.extend([
            f"- {idx}. {candidate.symbol}",
            f"  - Label: {candidate.label}",
            f"  - Score: {candidate.score}/100 | Technical {candidate.technical_score}/60 | Risk {candidate.risk_score}/20 | Fundamental/Sentiment context via Sonar",
            f"  - Entry condition: {candidate.entry_condition}",
            f"  - Stop reference: {render_money(candidate.stop_loss)}; 2R target reference: {render_money(candidate.target)}",
            f"  - Local facts: LTP {candidate.local_context.get('ltp')}; change {candidate.local_context.get('change')}; trend {candidate.local_context.get('trend')}; RSI {candidate.local_context.get('rsi_14')}; RelVol {candidate.local_context.get('relative_volume_20')}",
        ])
        lines.append("  - Technical reasons:")
        for reason in candidate.reasons[:6]:
            lines.append(f"    - {reason}")
        if candidate.risks:
            lines.append("  - Risks / review flags:")
            for risk in candidate.risks[:5]:
                lines.append(f"    - {risk}")
        note = deep_research_notes.get(candidate.symbol)
        if note:
            lines.append(f"  - Fundamental/sentiment: {note}")
        else:
            lines.append("  - Fundamental/sentiment: not refreshed in this run; use cached/live Sonar research before high-conviction decisions.")

    lines.extend([
        "",
        "## Safety / execution status",
        "- No orders placed, modified, or cancelled.",
        "- These are buy-candidate research signals, not automatic trade instructions.",
        "- Phase 2 algobot must remain paper-first until risk rules and explicit live-order approvals are implemented.",
    ])
    return "\n".join(lines)


def apply_migrations(conn: psycopg.Connection) -> None:
    for migration in ["002_deep_research_runs.sql", "003_morning_recommendations.sql"]:
        with conn.cursor() as cur:
            cur.execute((PROJECT_ROOT / "migrations" / migration).read_text())
    conn.commit()


def load_rows(conn: psycopg.Connection, symbols: Sequence[str] | None, limit: int) -> list[daily.ReportRow]:
    return daily.fetch_report_rows(conn, symbols, limit, "D")


def write_report(text: str, output: Path | None) -> Path:
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output = DEFAULT_OUTPUT_DIR / f"morning_stock_recommendations_{stamp}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text)
    return output


def store_signal_run(
    conn: psycopg.Connection,
    *,
    candidates: Sequence[Candidate],
    report_path: Path,
    params: dict[str, object],
    deep_research_enabled: bool,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into research.signal_runs(universe, live_orders_enabled, deep_research_enabled, params, report_path, status, notes)
            values (%s, false, %s, %s::jsonb, %s, 'success', %s)
            returning signal_run_id
            """,
            (
                str(params.get("watchlist") or "custom"),
                deep_research_enabled,
                json.dumps(params),
                str(report_path),
                "Morning recommendations generated; execution disabled.",
            ),
        )
        run_id = int(cur.fetchone()[0])
        for candidate in candidates:
            cur.execute(
                """
                insert into research.signals(
                    signal_run_id, symbol, label, score, technical_score, fundamental_score,
                    sentiment_score, risk_score, entry_condition, stop_loss, target,
                    max_risk_note, reasons, risks, local_context, deep_research_run_id
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)
                """,
                (
                    run_id,
                    candidate.symbol,
                    candidate.label,
                    candidate.score,
                    candidate.technical_score,
                    candidate.fundamental_score,
                    candidate.sentiment_score,
                    candidate.risk_score,
                    candidate.entry_condition,
                    candidate.stop_loss,
                    candidate.target,
                    candidate.max_risk_note,
                    json.dumps(candidate.reasons),
                    json.dumps(candidate.risks),
                    json.dumps(candidate.local_context),
                    candidate.deep_research_run_id,
                ),
            )
    conn.commit()
    return run_id


def run_deep_research_for_candidates(candidates: Sequence[Candidate], max_count: int, dry_run: bool, max_tokens: int) -> dict[str, str]:
    notes: dict[str, str] = {}
    selected = [c for c in candidates if c.label in {"buy_candidate_research", "paper_setup", "watch"}][:max_count]
    for candidate in selected:
        topic = candidate.symbol.replace("NSE:", "").replace("-EQ", "")
        output = DEFAULT_OUTPUT_DIR / f"deep_research_for_{topic}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_deep_research_context.py"),
            "--topic",
            topic,
            "--symbols",
            candidate.symbol,
            "--template",
            "stock_context",
            "--max-tokens",
            str(max_tokens),
            "--output",
            str(output),
        ]
        if dry_run:
            cmd.append("--dry-run")
        completed = subprocess.run(cmd, cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=1200)
        report_text = output.read_text() if output.exists() else ""
        if completed.returncode == 0 and "Status: error" not in report_text:
            notes[candidate.symbol] = f"Research report written to {output.name}. Review citations before acting."
        elif "Status: error" in report_text:
            notes[candidate.symbol] = f"Deep research returned an error report at {output.name}; do not treat fundamental/sentiment as refreshed."
        else:
            notes[candidate.symbol] = f"Deep research failed/skipped: {completed.stderr.strip()[:180] or completed.stdout.strip()[:180]}"
    return notes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate read-only 11 AM stock buy-candidate recommendations")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--symbols", nargs="*", help="Optional symbol override, e.g. RELIANCE or NSE:RELIANCE-EQ")
    parser.add_argument("--candidate-limit", type=int, default=10)
    parser.add_argument("--scan-limit", type=int, default=250)
    parser.add_argument("--apply-migrations", action="store_true")
    parser.add_argument("--deep-research-count", type=int, default=0, help="Run Sonar context for top N candidates")
    parser.add_argument("--dry-run-deep-research", action="store_true")
    parser.add_argument("--deep-research-max-tokens", type=int, default=8000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--print", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.candidate_limit <= 0:
        raise SystemExit("--candidate-limit must be positive")
    if args.scan_limit <= 0:
        raise SystemExit("--scan-limit must be positive")
    if args.deep_research_count < 0:
        raise SystemExit("--deep-research-count cannot be negative")


def main() -> None:
    args = parse_args()
    validate_args(args)
    symbols = deep_research.normalize_symbols(args.symbols)
    if not symbols and args.watchlist.exists():
        symbols = [row.fyers_symbol for row in watchlist_utils.load_watchlist(args.watchlist)]
    generated_at = datetime.now(timezone.utc)
    with psycopg.connect(DATABASE_URL) as conn:
        if args.apply_migrations:
            apply_migrations(conn)
        rows = load_rows(conn, symbols or None, args.scan_limit)
        candidates = select_candidates(rows, generated_at, args.candidate_limit)
        research_notes = run_deep_research_for_candidates(
            candidates,
            max_count=args.deep_research_count,
            dry_run=args.dry_run_deep_research,
            max_tokens=args.deep_research_max_tokens,
        ) if args.deep_research_count else {}
        text = render_recommendation_report(candidates, generated_at, research_notes)
        path = write_report(text, args.output)
        run_id = store_signal_run(
            conn,
            candidates=candidates,
            report_path=path,
            params={
                "watchlist": str(args.watchlist),
                "candidate_limit": args.candidate_limit,
                "scan_limit": args.scan_limit,
                "deep_research_count": args.deep_research_count,
            },
            deep_research_enabled=bool(args.deep_research_count),
        )
    if args.print:
        print(text)
    print(f"Stored signal run: {run_id}")
    print(f"Wrote morning recommendations: {path}")
    print("Read-only research recommendations; No orders placed.")


if __name__ == "__main__":
    main()
