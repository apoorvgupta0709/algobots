#!/usr/bin/env python3
"""Run source-backed OpenRouter Sonar Deep Research for stock/sector analysis.

This script combines local FYERS/Postgres facts with external deep research.
It is research-only: it does not place, modify, or cancel orders.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import generate_daily_market_report as market_report

DEFAULT_DATABASE_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"
FINANCE_PROFILE_ENV = Path("/opt/data/profiles/finance/.env")
DEFAULT_MODEL = "perplexity/sonar-deep-research"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OUTPUT_FORMAT = "markdown_report"

PROMPT_TEMPLATES: dict[str, str] = {
    "stock_context": """
Research the Indian listed company/stock: {topic}.
Symbols: {symbols}.
Focus on the last {lookback_days} days.

Use credible public sources and cite them. Separate confirmed facts from interpretation.
Cover:
1. Business/news summary
2. Earnings, filings, management commentary, corporate actions, or regulatory events
3. Sector tailwinds/headwinds
4. Bull case for a research watchlist
5. Bear case and key risks
6. Catalysts to monitor in the next 1-8 weeks
7. What would invalidate a bullish trading thesis

Local market context from FYERS/Postgres is below; treat it as the market-data source of truth:
{local_context}
""".strip(),
    "move_explanation": """
Explain why this Indian stock or group may have moved recently: {topic}.
Symbols: {symbols}.
Focus on the last {lookback_days} days.

Use credible recent sources. Separate confirmed facts from speculation.
Return:
1. Most likely drivers
2. Supporting evidence with dates/sources
3. Contradictory evidence or uncertainty
4. Whether the move appears company-specific, sector-wide, or market-wide
5. Risks to chasing the move

Local FYERS/Postgres context:
{local_context}
""".strip(),
    "sector_context": """
Research the Indian sector/theme: {topic}.
Relevant symbols: {symbols}.
Focus on the last {lookback_days} days and the next 1-2 quarter outlook.

Use credible public sources and cite them. Cover:
1. Demand/margin/regulatory backdrop
2. Recent news and events
3. Listed winners/laggards
4. Key catalysts
5. Key risks
6. Implications for watchlist screening, not trade execution

Local FYERS/Postgres context for relevant symbols:
{local_context}
""".strip(),
    "strategy_validation": """
Evaluate this trading/research strategy idea using public evidence and market-structure context:
{topic}

Relevant symbols/universe: {symbols}.
Lookback/context window: {lookback_days} days.

Return:
1. Supporting evidence
2. Contradictory evidence
3. Market regime where this may work
4. Failure modes and overfitting risks
5. Data needed for a backtest
6. Safe next step: watch/research/paper/backtest only

Local FYERS/Postgres context:
{local_context}
""".strip(),
}


@dataclass(frozen=True)
class DeepResearchResult:
    topic: str
    symbols: list[str]
    prompt_template: str
    query: str
    answer: str
    citations: list[dict[str, Any]]
    model: str
    provider: str
    output_format: str
    usage: dict[str, Any]
    cost: Decimal | None
    finish_reason: str | None
    status: str
    error: str | None
    raw: dict[str, Any]
    report_path: str | None = None
    created_at: datetime | None = None


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_openrouter_api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if key:
        return key
    for env_path in (PROJECT_ROOT / ".env", FINANCE_PROFILE_ENV):
        values = parse_env_file(env_path)
        key = values.get("OPENROUTER_API_KEY")
        if key:
            return key
    raise SystemExit("OPENROUTER_API_KEY is missing; add it to the finance profile .env and restart/reload Hermes.")


def normalize_symbols(symbols: Sequence[str] | None) -> list[str]:
    cleaned: list[str] = []
    for symbol in symbols or []:
        value = symbol.strip().upper()
        if not value:
            continue
        if ":" not in value and value.isalnum():
            value = f"NSE:{value}-EQ"
        cleaned.append(value)
    return sorted(dict.fromkeys(cleaned))


def fetch_local_context(conn: psycopg.Connection, symbols: Sequence[str], limit: int = 5) -> str:
    if not symbols:
        return "No symbol-level local market context requested."
    rows = market_report.fetch_report_rows(conn, symbols, limit=max(limit, len(symbols)), resolution="D")
    if not rows:
        return "No local factor/quote rows found for the requested symbols."
    lines: list[str] = []
    generated_at = datetime.now(timezone.utc)
    for row in rows:
        change, pct = market_report.quote_change(row)
        flags = market_report.classify_flags(row, generated_at)[:4]
        lines.extend(
            [
                f"- {row.symbol}: LTP {market_report.format_inr(row.ltp)}, change {market_report.format_inr(change)} ({market_report.format_pct(pct)}), trend {row.trend or 'n/a'}, RSI14 {market_report.format_decimal(row.rsi_14)}, ATR% {market_report.format_pct(row.atr_pct_14)}, RelVol20 {market_report.format_decimal(row.relative_volume_20)}.",
                f"  Freshness: {market_report.freshness_summary(row, generated_at)}.",
                f"  Technical flags: {'; '.join(flags)}.",
            ]
        )
    return "\n".join(lines)


def build_query(
    *,
    topic: str,
    symbols: Sequence[str],
    prompt_template: str,
    local_context: str,
    lookback_days: int,
    custom_query: str | None = None,
) -> str:
    if custom_query:
        return custom_query.strip() + "\n\nLocal FYERS/Postgres context:\n" + local_context
    if prompt_template not in PROMPT_TEMPLATES:
        raise SystemExit(f"Unknown prompt template: {prompt_template}")
    return PROMPT_TEMPLATES[prompt_template].format(
        topic=topic.strip(),
        symbols=", ".join(symbols) if symbols else "not specified",
        local_context=local_context,
        lookback_days=lookback_days,
    )


def extract_citations(response: dict[str, Any]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []

    def add_citation(url: str | None, title: str | None = None, raw: Any = None) -> None:
        if not url:
            return
        item = {"url": url}
        if title:
            item["title"] = title
        if raw is not None:
            item["raw"] = raw
        if item["url"] not in {c.get("url") for c in citations}:
            citations.append(item)

    for item in response.get("citations") or []:
        if isinstance(item, str):
            add_citation(item)
        elif isinstance(item, dict):
            add_citation(item.get("url"), item.get("title"), item)

    choices = response.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        for annotation in message.get("annotations") or []:
            if not isinstance(annotation, dict):
                continue
            url_data = annotation.get("url_citation") or annotation.get("citation") or annotation
            if isinstance(url_data, dict):
                add_citation(url_data.get("url"), url_data.get("title"), annotation)
        content = message.get("content") or ""
        for url in re.findall(r"https?://[^\s)\]]+", content):
            add_citation(url.rstrip(".,;"))

    return citations


def parse_openrouter_response(
    response: dict[str, Any],
    *,
    topic: str,
    symbols: Sequence[str],
    prompt_template: str,
    query: str,
    output_format: str,
) -> DeepResearchResult:
    choices = response.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    answer = message.get("content") or ""
    finish_reason = choices[0].get("finish_reason") if choices else None
    usage = response.get("usage") or {}
    raw_cost = usage.get("cost") if isinstance(usage, dict) else None
    cost = Decimal(str(raw_cost)) if raw_cost is not None else None
    return DeepResearchResult(
        topic=topic,
        symbols=list(symbols),
        prompt_template=prompt_template,
        query=query,
        answer=answer,
        citations=extract_citations(response),
        model=response.get("model") or DEFAULT_MODEL,
        provider=response.get("provider") or "openrouter",
        output_format=output_format,
        usage=usage if isinstance(usage, dict) else {},
        cost=cost,
        finish_reason=finish_reason,
        status="success" if answer else "error",
        error=None if answer else "OpenRouter response did not include answer content",
        raw=response,
    )


def call_openrouter(
    *,
    query: str,
    model: str,
    output_format: str,
    max_tokens: int,
    temperature: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    key = get_openrouter_api_key()
    system = (
        "You are a source-backed financial research assistant. "
        "Separate facts from interpretation. Do not give investment advice or order instructions. "
        "For Indian stocks, treat provided FYERS/Postgres market data as the source of truth for price/technical facts."
    )
    if output_format == "brief":
        system += " Keep the answer concise."
    elif output_format == "sources_first":
        system += " Put sources/citations first, then analysis."
    else:
        system += " Return a structured markdown report with sections and citations."

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        "max_tokens": max(512, min(max_tokens, 32000)),
        "temperature": temperature,
    }
    request = urllib.request.Request(
        DEFAULT_BASE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "Hermes Finance Deep Research",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise SystemExit(f"OpenRouter HTTP {exc.code}: {body}") from exc


def apply_migration(conn: psycopg.Connection) -> None:
    migration = PROJECT_ROOT / "migrations" / "002_deep_research_runs.sql"
    with conn.cursor() as cur:
        cur.execute(migration.read_text())
    conn.commit()


def find_cached_run(
    conn: psycopg.Connection,
    *,
    topic: str,
    symbols: Sequence[str],
    prompt_template: str,
    max_age_hours: int,
) -> DeepResearchResult | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select topic, symbols, prompt_template, query, coalesce(answer, ''), citations, model,
                   provider, output_format, usage, cost, finish_reason, status, error, raw, report_path, created_at
            from research.deep_research_runs
            where topic = %s
              and prompt_template = %s
              and symbols = %s::text[]
              and status = 'success'
              and created_at >= now() - (%s * interval '1 hour')
            order by created_at desc
            limit 1
            """,
            (topic, prompt_template, list(symbols), max_age_hours),
        )
        row = cur.fetchone()
    if not row:
        return None
    return DeepResearchResult(
        topic=row[0],
        symbols=list(row[1] or []),
        prompt_template=row[2],
        query=row[3],
        answer=row[4],
        citations=list(row[5] or []),
        model=row[6],
        provider=row[7],
        output_format=row[8],
        usage=dict(row[9] or {}),
        cost=row[10],
        finish_reason=row[11],
        status=row[12],
        error=row[13],
        raw=dict(row[14] or {}),
        report_path=row[15],
        created_at=row[16],
    )


def store_run(conn: psycopg.Connection, result: DeepResearchResult) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into research.deep_research_runs(
                topic, symbols, prompt_template, query, answer, citations, model, provider,
                output_format, usage, cost, finish_reason, status, error, raw, report_path
            ) values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb, %s)
            returning deep_research_run_id
            """,
            (
                result.topic,
                result.symbols,
                result.prompt_template,
                result.query,
                result.answer,
                json.dumps(result.citations),
                result.model,
                result.provider,
                result.output_format,
                json.dumps(result.usage),
                result.cost,
                result.finish_reason,
                result.status,
                result.error,
                json.dumps(result.raw),
                result.report_path,
            ),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return int(run_id)


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return slug.strip("_")[:80] or "deep_research"


def render_deep_research_report(result: DeepResearchResult, local_context: str, cached: bool = False) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    source_lines = []
    for idx, citation in enumerate(result.citations[:12], 1):
        title = citation.get("title") or citation.get("url")
        source_lines.append(f"- {idx}. {title}: {citation.get('url')}")
    if not source_lines:
        source_lines.append("- No citations extracted from response; inspect raw output before relying on it.")

    usage_bits = []
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost"):
        if key in result.usage:
            usage_bits.append(f"{key}: {result.usage[key]}")
    usage_text = "; ".join(usage_bits) if usage_bits else "n/a"

    return "\n".join(
        [
            "## Deep Research Stock/Market Context",
            f"Generated: {generated_at}",
            "Scope: read-only research context; Not trade advice; no orders placed.",
            f"Status: {'cached' if cached else result.status}",
            f"Topic: {result.topic}",
            f"Symbols: {', '.join(result.symbols) if result.symbols else 'not specified'}",
            f"Template: {result.prompt_template}",
            "",
            "## Local FYERS/Postgres facts",
            local_context,
            "",
            "## External research synthesis",
            result.answer or result.error or "No answer returned.",
            "",
            "## Sources extracted",
            *source_lines,
            "",
            "## Model / usage",
            f"- Provider/model: {result.provider} / {result.model}",
            f"- Finish reason: {result.finish_reason or 'n/a'}",
            f"- Usage: {usage_text}",
            "",
            "## Suggested next actions",
            "- Treat this as evidence context only; confirm price/technical facts from local FYERS data.",
            "- If the thesis is useful, create a separate `watch`, `research`, `paper`, or `backtest` idea; no live execution is implied.",
        ]
    )


def write_report(text: str, output_path: Path | None, topic: str) -> Path:
    if output_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = PROJECT_ROOT / "reports" / f"deep_research_{slugify(topic)}_{stamp}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Sonar Deep Research with local FYERS/Postgres context")
    parser.add_argument("--topic", required=True, help="Company, sector, event, or strategy idea to research")
    parser.add_argument("--symbols", nargs="*", help="Optional NSE/FYERS symbols, e.g. RELIANCE or NSE:RELIANCE-EQ")
    parser.add_argument("--template", default="stock_context", choices=sorted(PROMPT_TEMPLATES), help="Prompt template")
    parser.add_argument("--query", help="Custom query; local context is appended")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--max-age-hours", type=int, default=48, help="Reuse successful cached research within this many hours")
    parser.add_argument("--force", action="store_true", help="Ignore cache and call OpenRouter")
    parser.add_argument("--dry-run", action="store_true", help="Print/render the prompt without calling OpenRouter or storing a run")
    parser.add_argument("--mock-response-json", type=Path, help="Use a local OpenRouter-style JSON response instead of a live API call")
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--output-format", default=DEFAULT_OUTPUT_FORMAT, choices=["markdown_report", "brief", "sources_first"])
    parser.add_argument("--output", type=Path, help="Markdown report path")
    parser.add_argument("--print", action="store_true", help="Print report/prompt to stdout")
    parser.add_argument("--apply-migration", action="store_true", help="Apply migrations/002_deep_research_runs.sql before running")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.lookback_days <= 0:
        raise SystemExit("--lookback-days must be positive")
    if args.max_age_hours <= 0:
        raise SystemExit("--max-age-hours must be positive")
    if args.max_tokens < 512 or args.max_tokens > 32000:
        raise SystemExit("--max-tokens must be between 512 and 32000")
    if args.temperature < 0 or args.temperature > 2:
        raise SystemExit("--temperature must be between 0 and 2")


def main() -> None:
    args = parse_args()
    validate_args(args)
    symbols = normalize_symbols(args.symbols)
    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

    with psycopg.connect(database_url) as conn:
        if args.apply_migration:
            apply_migration(conn)
        local_context = fetch_local_context(conn, symbols)
        query = build_query(
            topic=args.topic,
            symbols=symbols,
            prompt_template=args.template,
            local_context=local_context,
            lookback_days=args.lookback_days,
            custom_query=args.query,
        )

        if args.dry_run:
            dry_result = DeepResearchResult(
                topic=args.topic,
                symbols=symbols,
                prompt_template=args.template,
                query=query,
                answer="DRY RUN: no OpenRouter call was made.",
                citations=[],
                model=DEFAULT_MODEL,
                provider="openrouter",
                output_format=args.output_format,
                usage={},
                cost=None,
                finish_reason=None,
                status="dry_run",
                error=None,
                raw={},
            )
            text = render_deep_research_report(dry_result, local_context)
            path = write_report(text, args.output, args.topic)
            if args.print:
                print(text)
            print(f"Wrote dry-run deep research report: {path}")
            print("No OpenRouter API call made; no DB run stored.")
            return

        cached = None if args.force else find_cached_run(
            conn,
            topic=args.topic,
            symbols=symbols,
            prompt_template=args.template,
            max_age_hours=args.max_age_hours,
        )
        if cached:
            text = render_deep_research_report(cached, local_context, cached=True)
            path = write_report(text, args.output, args.topic)
            if args.print:
                print(text)
            print(f"Wrote cached deep research report: {path}")
            return

        if args.mock_response_json:
            response = json.loads(args.mock_response_json.read_text())
        else:
            response = call_openrouter(
                query=query,
                model=DEFAULT_MODEL,
                output_format=args.output_format,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                timeout_seconds=args.timeout_seconds,
            )
        result = parse_openrouter_response(
            response,
            topic=args.topic,
            symbols=symbols,
            prompt_template=args.template,
            query=query,
            output_format=args.output_format,
        )
        text = render_deep_research_report(result, local_context)
        path = write_report(text, args.output, args.topic)
        result = DeepResearchResult(**{**result.__dict__, "report_path": str(path)})
        run_id = store_run(conn, result)
        if args.print:
            print(text)
        print(f"Stored deep research run: {run_id}")
        print(f"Wrote deep research report: {path}")


if __name__ == "__main__":
    main()
