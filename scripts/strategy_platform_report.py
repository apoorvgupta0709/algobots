#!/usr/bin/env python3
"""Operational Markdown report for the India strategy platform (paper/research only).

Loads the strategy registry (``config/strategy_universe_india.json``) and the
one-month paper-qualification config (``config/strategy_qualification.json``) and
prints a Markdown summary covering:

- platform safety posture (paper-only, no live orders),
- per-desk counts and executable vs scorecard-only split,
- the lifecycle-status histogram (the backtest -> paper -> qualified funnel),
- the aggregate rupee risk envelope across executable strategies,
- the one-month paper-trial requirements, and
- the manual live-approval gate (the only path to a live-eligibility *label*).

Safety stance:
- Pure and read-only: reads two JSON files, nothing else. No database, no FYERS /
  broker calls, no order placement/modification/cancellation, no network, no LLM,
  no secret/.env loading. It cannot change any strategy's status.

CLI:
    uv run python scripts/strategy_platform_report.py
    uv run python scripts/strategy_platform_report.py --output
    uv run python scripts/strategy_platform_report.py --output reports/strategy_platform_report.md
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.strategy_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    LIFECYCLE_ORDER,
    Desk,
    LifecycleStatus,
    StrategyUniverse,
    load_registry,
)
from scripts.strategy_qualification import (  # noqa: E402
    LIVE_ELIGIBILITY_PHRASE,
    QualificationCriteria,
)

DEFAULT_QUALIFICATION_CONFIG = PROJECT_ROOT / "config" / "strategy_qualification.json"
REPORTS_DIR = PROJECT_ROOT / "reports"
DEFAULT_OUTPUT_PATH = REPORTS_DIR / "strategy_platform_report.md"


def load_qualification_config(path: Path | None) -> tuple[QualificationCriteria, dict[str, Any]]:
    """Load the qualification config, tolerating a missing file (defaults) safely."""
    if path is None:
        return QualificationCriteria(), {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return QualificationCriteria(), {}
    if not isinstance(raw, dict):
        return QualificationCriteria(), {}
    return QualificationCriteria.from_dict(raw), raw


def _risk_envelope(universe: StrategyUniverse) -> dict[str, tuple[str, str]]:
    """Min/max of each rupee/int risk cap across executable strategies (Decimal-safe)."""
    executables = [s for s in universe.executable_strategies() if s.risk is not None]
    if not executables:
        return {}
    decimal_keys = ("paper_capital", "max_trade_loss", "max_daily_loss", "max_premium_exposure")
    int_keys = ("max_trades_per_day", "max_open_positions")
    envelope: dict[str, tuple[str, str]] = {}
    for key in decimal_keys:
        values = [getattr(s.risk, key) for s in executables]
        lo, hi = min(values), max(values)
        envelope[key] = (f"{lo:.2f}", f"{hi:.2f}")
    for key in int_keys:
        values = [getattr(s.risk, key) for s in executables]
        envelope[key] = (str(min(values)), str(max(values)))
    return envelope


def _fmt_range(lo: str, hi: str, *, money: bool) -> str:
    prefix = "₹" if money else ""
    if lo == hi:
        return f"{prefix}{lo}"
    return f"{prefix}{lo} – {prefix}{hi}"


def report_lines(
    universe: StrategyUniverse,
    criteria: QualificationCriteria,
    raw_config: dict[str, Any],
    *,
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    config_path: Path | str | None = DEFAULT_QUALIFICATION_CONFIG,
) -> list[str]:
    total = len(universe.strategies)
    executable = universe.executable_strategies()
    scorecard = universe.scorecard_strategies()
    live_labelled = [s for s in universe.strategies if s.is_live_eligible]

    lines: list[str] = [
        "# India Strategy Platform — Operational Report",
        "",
        "> Paper/research only. No live broker orders are placed anywhere in this system.",
        "> This report reads the registry and qualification config only; it makes no",
        "> database, broker/FYERS, network, or LLM calls and changes no strategy status.",
        "",
        f"- Registry: `{registry_path}` (schema {universe.schema_version})",
        f"- Qualification config: `{config_path if config_path else 'built-in defaults'}`",
        f"- Total strategies: **{total}** — {len(executable)} executable, {len(scorecard)} scorecard-only",
        "",
        "## Safety posture",
        "",
        f"- Registry `paper_only`: **{universe.paper_only}**",
        f"- Registry `live_orders_enabled`: **{universe.live_orders_enabled}**",
        f"- Strategies not paper-only: **{sum(1 for s in universe.strategies if s.paper_only is not True)}** (must be 0)",
        f"- Executable option-selling / short-premium strategies: "
        f"**{sum(1 for s in universe.strategies if s.executable and s.option_selling)}** (must be 0)",
        f"- Strategies pre-labelled live-eligible in the file: **{len(live_labelled)}** "
        f"(must be 0; that label is manual-only)",
        "",
        "## Desks",
        "",
        "| Desk | Total | Executable | Scorecard-only |",
        "| --- | ---: | ---: | ---: |",
    ]
    for desk in Desk:
        desk_strategies = universe.by_desk(desk)
        if not desk_strategies:
            continue
        info = universe.desks.get(desk)
        ex = sum(1 for s in desk_strategies if s.executable)
        name = info.name if info else desk.value
        lines.append(f"| {name} | {len(desk_strategies)} | {ex} | {len(desk_strategies) - ex} |")

    lines.extend(["", "## Lifecycle funnel", "",
                  "Backtest → paper → qualified progression. The terminal "
                  "`live_eligible_requires_manual_approval` status is a governance label only "
                  "and is never set by the file or by automation.", "",
                  "| Lifecycle status | Count |", "| --- | ---: |"])
    histogram = universe.lifecycle_histogram()
    for status in LIFECYCLE_ORDER:
        lines.append(f"| {status.value} | {histogram.get(status.value, 0)} |")

    lines.extend(["", "## Aggregate risk envelope (executable strategies)", ""])
    envelope = _risk_envelope(universe)
    if not envelope:
        lines.append("_No executable strategies carry a risk block._")
    else:
        lines.extend([
            "Per-strategy rupee risk caps (min–max across all executable strategies). "
            "Every executable strategy defines its own caps; none may enable live orders.",
            "",
            "| Risk cap | Range |",
            "| --- | --- |",
            f"| Paper capital | {_fmt_range(*envelope['paper_capital'], money=True)} |",
            f"| Max loss / trade | {_fmt_range(*envelope['max_trade_loss'], money=True)} |",
            f"| Max daily loss | {_fmt_range(*envelope['max_daily_loss'], money=True)} |",
            f"| Max premium / exposure | {_fmt_range(*envelope['max_premium_exposure'], money=True)} |",
            f"| Max trades / day | {_fmt_range(*envelope['max_trades_per_day'], money=False)} |",
            f"| Max open positions | {_fmt_range(*envelope['max_open_positions'], money=False)} |",
        ])

    trial_months = raw_config.get("paper_trial_calendar_months", 1)
    lines.extend([
        "",
        "## One-month paper-trial requirements",
        "",
        f"A strategy must run on paper for **{trial_months} calendar month** and clear every "
        "threshold below before the engine will *recommend* advancing it to `qualified`:",
        "",
        "| Criterion | Threshold |",
        "| --- | --- |",
        f"| Min closed trades | {criteria.min_closed_trades} |",
        f"| Min trading days | {criteria.min_trading_days} |",
        f"| Min win rate | {criteria.min_win_rate:.2f}% |",
        f"| Min profit factor | {criteria.min_profit_factor:.2f} |",
        f"| Min net P&L | ₹{criteria.min_net_pnl:.2f} |",
        f"| Max drawdown | ₹{criteria.max_drawdown:.2f} |",
        f"| Min expectancy / trade | ₹{criteria.min_expectancy:.2f} |",
    ])

    lines.extend([
        "",
        "## Manual live-approval gate",
        "",
        "A passing one-month trial only *recommends* `qualified`. The single path to the "
        "`live_eligible_requires_manual_approval` label requires **all** of:",
        "",
        "1. the strategy is executable (scorecard-only strategies can never be made live-eligible),",
        "2. its current status is `qualified`,",
        "3. a named human approver,",
        f"4. the exact confirmation phrase `{LIVE_ELIGIBILITY_PHRASE.format(strategy_id='<strategy_id>')}`, and",
        "5. an explicit acknowledgement flag.",
        "",
        "Even when granted it returns a governance record only — **no order-placement, "
        "modification, cancellation, or exit code is enabled anywhere in this repository.**",
        "",
    ])
    return lines


def build_report(
    universe: StrategyUniverse,
    criteria: QualificationCriteria,
    raw_config: dict[str, Any],
    *,
    registry_path: Path | str = DEFAULT_REGISTRY_PATH,
    config_path: Path | str | None = DEFAULT_QUALIFICATION_CONFIG,
) -> str:
    return "\n".join(
        report_lines(universe, criteria, raw_config, registry_path=registry_path, config_path=config_path)
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print an operational Markdown report for the India strategy platform (paper-only, read-only)."
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--qualification-config", type=Path, default=DEFAULT_QUALIFICATION_CONFIG)
    parser.add_argument(
        "--output",
        type=Path,
        nargs="?",
        const=DEFAULT_OUTPUT_PATH,
        default=None,
        help="write the report to this path (default reports/strategy_platform_report.md when the flag is given)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        universe = load_registry(args.registry)
    except Exception as exc:  # RegistryError or IO
        print(f"Cannot load registry: {exc}")
        return 1
    criteria, raw_config = load_qualification_config(args.qualification_config)
    report = build_report(
        universe, criteria, raw_config,
        registry_path=args.registry, config_path=args.qualification_config,
    )
    print(report)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
