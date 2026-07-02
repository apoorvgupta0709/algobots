#!/usr/bin/env python3
"""One-month paper-qualification engine for the India strategy platform.

Given paper-trade rows (from a read-only export, a JSON/CSV file, or the built-in
sample set), this computes Decimal-safe performance metrics, evaluates a one-month
paper trial against configurable criteria, and emits a Markdown + CSV report.

Safety stance:
- Read-only. Never connects to a broker; never places/modifies/cancels orders.
- Input is loaded from a file or the sample set; it never writes to the trading DB.
- A passing trial can only *recommend* advancing to ``qualified``. Promotion to
  ``live_eligible_requires_manual_approval`` is **never automatic** — it requires an
  explicit human approval (:func:`grant_live_eligibility`) and even then no order
  code is enabled anywhere.

CLI:
    uv run python scripts/strategy_qualification.py --mode sample
    uv run python scripts/strategy_qualification.py --input trades.json --from 2026-06-01
    uv run python scripts/strategy_qualification.py --grant-live-eligibility \
        --strategy-id nifty_orb_debit_spread --approved-by "Apoorv" \
        --confirm "APPROVE LIVE ELIGIBILITY nifty_orb_debit_spread" --acknowledge
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.strategy_registry import (  # noqa: E402
    LifecycleStatus,
    StrategyDefinition,
    load_registry,
)

IST = ZoneInfo("Asia/Kolkata")
TWO_PLACES = Decimal("0.01")
REPORTS_DIR = PROJECT_ROOT / "reports"

# The exact confirmation phrase a human must type to grant live eligibility.
LIVE_ELIGIBILITY_PHRASE = "APPROVE LIVE ELIGIBILITY {strategy_id}"


class QualificationError(ValueError):
    """Raised for malformed inputs or invalid manual-approval requests."""


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_ist_datetime(value: Any) -> datetime | None:
    """Parse a datetime and normalize it to IST. Naive inputs are assumed IST."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def add_one_month(day: date) -> date:
    """Return the same day-of-month one calendar month later, clamped to month end."""
    month = day.month + 1
    year = day.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    # Clamp overflowing days (e.g. Jan 31 -> Feb 28/29); days <= 28 always exist.
    candidate = day.day
    while candidate > 28:
        try:
            return date(year, month, candidate)
        except ValueError:
            candidate -= 1
    return date(year, month, candidate)


@dataclass(frozen=True)
class PaperTrade:
    strategy_id: str
    status: str
    realized_pnl: Decimal | None
    entry_time: datetime | None
    exit_time: datetime | None
    quantity: int = 0

    @property
    def is_closed(self) -> bool:
        return self.status == "closed" and self.realized_pnl is not None


def normalize_trade(row: dict[str, Any]) -> PaperTrade:
    strategy_id = row.get("strategy_id") or row.get("strategy_version") or row.get("strategy") or "unknown"
    quantity_raw = row.get("quantity")
    try:
        quantity = int(quantity_raw) if quantity_raw not in (None, "") else 0
    except (TypeError, ValueError):
        quantity = 0
    return PaperTrade(
        strategy_id=str(strategy_id),
        status=str(row.get("status") or "").lower(),
        realized_pnl=_to_decimal(row.get("realized_pnl")),
        entry_time=parse_ist_datetime(row.get("entry_time")),
        exit_time=parse_ist_datetime(row.get("exit_time")),
        quantity=quantity,
    )


def normalize_trades(rows: Iterable[dict[str, Any]]) -> list[PaperTrade]:
    return [normalize_trade(row) for row in rows]


@dataclass(frozen=True)
class QualificationMetrics:
    strategy_id: str
    total_trades: int
    closed_trades: int
    open_trades: int
    wins: int
    losses: int
    win_rate: Decimal          # percent, 0-100
    gross_profit: Decimal
    gross_loss: Decimal        # positive magnitude of losing trades
    net_pnl: Decimal
    avg_win: Decimal
    avg_loss: Decimal          # positive magnitude
    avg_trade: Decimal
    profit_factor: Decimal | None   # None when gross_loss == 0
    expectancy: Decimal
    largest_win: Decimal
    largest_loss: Decimal      # negative or zero
    max_drawdown: Decimal      # positive magnitude of worst peak-to-trough
    max_consecutive_losses: int
    trading_days: int

    def as_display(self) -> dict[str, str]:
        return {
            "strategy_id": self.strategy_id,
            "total_trades": str(self.total_trades),
            "closed_trades": str(self.closed_trades),
            "open_trades": str(self.open_trades),
            "wins": str(self.wins),
            "losses": str(self.losses),
            "win_rate_pct": f"{self.win_rate:.2f}",
            "gross_profit": f"{self.gross_profit:.2f}",
            "gross_loss": f"{self.gross_loss:.2f}",
            "net_pnl": f"{self.net_pnl:.2f}",
            "avg_win": f"{self.avg_win:.2f}",
            "avg_loss": f"{self.avg_loss:.2f}",
            "avg_trade": f"{self.avg_trade:.2f}",
            "profit_factor": "inf" if self.profit_factor is None else f"{self.profit_factor:.2f}",
            "expectancy": f"{self.expectancy:.2f}",
            "largest_win": f"{self.largest_win:.2f}",
            "largest_loss": f"{self.largest_loss:.2f}",
            "max_drawdown": f"{self.max_drawdown:.2f}",
            "max_consecutive_losses": str(self.max_consecutive_losses),
            "trading_days": str(self.trading_days),
        }


def _sort_key(trade: PaperTrade) -> tuple[datetime, datetime]:
    far_future = datetime.max.replace(tzinfo=IST)
    return (trade.exit_time or far_future, trade.entry_time or far_future)


def compute_metrics(strategy_id: str, trades: Sequence[PaperTrade]) -> QualificationMetrics:
    """Compute Decimal-safe metrics from paper trades for a single strategy."""
    relevant = [t for t in trades if t.strategy_id == strategy_id]
    closed = sorted([t for t in relevant if t.is_closed], key=_sort_key)
    open_trades = sum(1 for t in relevant if t.status == "open")

    zero = Decimal("0")
    if not closed:
        return QualificationMetrics(
            strategy_id=strategy_id, total_trades=len(relevant), closed_trades=0,
            open_trades=open_trades, wins=0, losses=0, win_rate=zero, gross_profit=zero,
            gross_loss=zero, net_pnl=zero, avg_win=zero, avg_loss=zero, avg_trade=zero,
            profit_factor=None, expectancy=zero, largest_win=zero, largest_loss=zero,
            max_drawdown=zero, max_consecutive_losses=0, trading_days=0,
        )

    pnls = [t.realized_pnl for t in closed if t.realized_pnl is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins, zero)
    gross_loss = -sum(losses, zero)  # positive magnitude
    net_pnl = sum(pnls, zero)
    closed_count = len(pnls)

    win_rate = (Decimal(len(wins)) / Decimal(closed_count) * Decimal("100")) if closed_count else zero
    avg_win = (gross_profit / Decimal(len(wins))) if wins else zero
    avg_loss = (gross_loss / Decimal(len(losses))) if losses else zero
    avg_trade = (net_pnl / Decimal(closed_count)) if closed_count else zero
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    expectancy = avg_trade  # per-trade expected P&L

    # Max drawdown on the cumulative realized-equity curve.
    equity = zero
    peak = zero
    max_dd = zero
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_dd = max(max_dd, drawdown)

    # Longest losing streak.
    max_streak = 0
    streak = 0
    for pnl in pnls:
        if pnl < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    trading_days = len({t.exit_time.date() for t in closed if t.exit_time is not None})

    return QualificationMetrics(
        strategy_id=strategy_id,
        total_trades=len(relevant),
        closed_trades=closed_count,
        open_trades=open_trades,
        wins=len(wins),
        losses=len(losses),
        win_rate=q2(win_rate),
        gross_profit=q2(gross_profit),
        gross_loss=q2(gross_loss),
        net_pnl=q2(net_pnl),
        avg_win=q2(avg_win),
        avg_loss=q2(avg_loss),
        avg_trade=q2(avg_trade),
        profit_factor=q2(profit_factor) if profit_factor is not None else None,
        expectancy=q2(expectancy),
        largest_win=q2(max(wins)) if wins else zero,
        largest_loss=q2(min(losses)) if losses else zero,
        max_drawdown=q2(max_dd),
        max_consecutive_losses=max_streak,
        trading_days=trading_days,
    )


@dataclass(frozen=True)
class TrialWindow:
    start: date
    end: date  # exclusive

    @classmethod
    def one_month_from(cls, start: date) -> TrialWindow:
        return cls(start=start, end=add_one_month(start))

    def contains(self, moment: datetime | None) -> bool:
        if moment is None:
            return False
        moment_ist = moment.astimezone(IST)
        start_dt = datetime(self.start.year, self.start.month, self.start.day, tzinfo=IST)
        end_dt = datetime(self.end.year, self.end.month, self.end.day, tzinfo=IST)
        return start_dt <= moment_ist < end_dt

    def progress_pct(self, as_of: date) -> Decimal:
        total = (self.end - self.start).days or 1
        elapsed = max(0, min(total, (as_of - self.start).days))
        return q2(Decimal(elapsed) / Decimal(total) * Decimal("100"))

    def is_complete(self, as_of: date) -> bool:
        return as_of >= self.end


@dataclass(frozen=True)
class QualificationCriteria:
    """Thresholds a one-month paper trial must clear to be *recommended* for qualification."""

    min_closed_trades: int = 15
    min_trading_days: int = 10
    min_win_rate: Decimal = Decimal("40")          # percent
    min_profit_factor: Decimal = Decimal("1.20")
    min_net_pnl: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("6000")        # positive cap
    min_expectancy: Decimal = Decimal("0")

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> QualificationCriteria:
        if not data:
            return cls()
        defaults = cls()
        return cls(
            min_closed_trades=int(data.get("min_closed_trades", defaults.min_closed_trades)),
            min_trading_days=int(data.get("min_trading_days", defaults.min_trading_days)),
            min_win_rate=Decimal(str(data.get("min_win_rate", defaults.min_win_rate))),
            min_profit_factor=Decimal(str(data.get("min_profit_factor", defaults.min_profit_factor))),
            min_net_pnl=Decimal(str(data.get("min_net_pnl", defaults.min_net_pnl))),
            max_drawdown=Decimal(str(data.get("max_drawdown", defaults.max_drawdown))),
            min_expectancy=Decimal(str(data.get("min_expectancy", defaults.min_expectancy))),
        )


@dataclass(frozen=True)
class CriterionResult:
    name: str
    passed: bool
    observed: str
    threshold: str


@dataclass(frozen=True)
class TrialEvaluation:
    strategy_id: str
    window: TrialWindow
    metrics: QualificationMetrics
    criteria: QualificationCriteria
    results: tuple[CriterionResult, ...]
    passed: bool
    current_status: LifecycleStatus | None
    recommended_status: LifecycleStatus | None
    recommendation_note: str


def evaluate_trial(
    strategy_id: str,
    trades: Sequence[PaperTrade],
    *,
    window: TrialWindow,
    criteria: QualificationCriteria | None = None,
    current_status: LifecycleStatus | None = None,
) -> TrialEvaluation:
    """Evaluate a one-month paper trial. Never auto-promotes past ``qualified``."""
    criteria = criteria or QualificationCriteria()
    in_window = [t for t in trades if t.strategy_id == strategy_id and window.contains(t.exit_time)]
    metrics = compute_metrics(strategy_id, in_window)

    pf_observed = "inf" if metrics.profit_factor is None else f"{metrics.profit_factor:.2f}"
    pf_passed = metrics.profit_factor is None or metrics.profit_factor >= criteria.min_profit_factor
    results = (
        CriterionResult("min_closed_trades", metrics.closed_trades >= criteria.min_closed_trades,
                        str(metrics.closed_trades), str(criteria.min_closed_trades)),
        CriterionResult("min_trading_days", metrics.trading_days >= criteria.min_trading_days,
                        str(metrics.trading_days), str(criteria.min_trading_days)),
        CriterionResult("min_win_rate", metrics.win_rate >= criteria.min_win_rate,
                        f"{metrics.win_rate:.2f}%", f"{criteria.min_win_rate:.2f}%"),
        CriterionResult("min_profit_factor", pf_passed, pf_observed, f"{criteria.min_profit_factor:.2f}"),
        CriterionResult("min_net_pnl", metrics.net_pnl >= criteria.min_net_pnl,
                        f"{metrics.net_pnl:.2f}", f"{criteria.min_net_pnl:.2f}"),
        CriterionResult("max_drawdown", metrics.max_drawdown <= criteria.max_drawdown,
                        f"{metrics.max_drawdown:.2f}", f"{criteria.max_drawdown:.2f}"),
        CriterionResult("min_expectancy", metrics.expectancy >= criteria.min_expectancy,
                        f"{metrics.expectancy:.2f}", f"{criteria.min_expectancy:.2f}"),
    )
    passed = all(result.passed for result in results)

    recommended_status, note = _recommend(passed, current_status)
    return TrialEvaluation(
        strategy_id=strategy_id, window=window, metrics=metrics, criteria=criteria,
        results=results, passed=passed, current_status=current_status,
        recommended_status=recommended_status, recommendation_note=note,
    )


def _recommend(passed: bool, current_status: LifecycleStatus | None) -> tuple[LifecycleStatus | None, str]:
    """Recommend the next lifecycle status. The ceiling is QUALIFIED — never live."""
    if not passed:
        if current_status is None:
            return None, "Trial not passed. Keep observing on paper; do not advance."
        return current_status, "Trial not passed. Hold at current status and keep collecting paper trades."
    # Passed: recommend advancing toward qualified, but never beyond it automatically.
    if current_status in (None, LifecycleStatus.PAPER_ENABLED, LifecycleStatus.PAPER_OBSERVING, LifecycleStatus.BACKTESTED):
        return LifecycleStatus.QUALIFIED, (
            "Trial passed. Recommend marking QUALIFIED. Live eligibility still requires a separate, "
            "explicit manual approval (grant_live_eligibility); it is never automatic."
        )
    if current_status is LifecycleStatus.QUALIFIED:
        return LifecycleStatus.QUALIFIED, (
            "Already QUALIFIED and trial still passing. Live eligibility requires explicit manual approval."
        )
    return current_status, "Trial passed. No automatic advancement from the current status."


# --------------------------------------------------------------------------- #
# Manual approval — the ONLY path to live_eligible, and it enables no orders.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ManualApprovalRecord:
    strategy_id: str
    approved_by: str
    confirmation_text: str
    previous_status: LifecycleStatus
    new_status: LifecycleStatus
    acknowledgement: str
    approved_at_ist: str


def expected_confirmation_phrase(strategy_id: str) -> str:
    return LIVE_ELIGIBILITY_PHRASE.format(strategy_id=strategy_id)


def grant_live_eligibility(
    strategy: StrategyDefinition,
    *,
    approved_by: str,
    confirmation_text: str,
    acknowledged: bool,
    as_of: datetime | None = None,
) -> ManualApprovalRecord:
    """Manually grant the ``live_eligible_requires_manual_approval`` label.

    This is the only way to reach that status and it is purely a governance label:
    it enables NO order placement. Preconditions are strict and all must be explicit.
    """
    if not strategy.executable:
        raise QualificationError(f"{strategy.id}: scorecard-only strategies can never be made live-eligible")
    if strategy.lifecycle_status is not LifecycleStatus.QUALIFIED:
        raise QualificationError(
            f"{strategy.id}: must be QUALIFIED before manual live-eligibility (current: {strategy.lifecycle_status.value})"
        )
    if not approved_by or not approved_by.strip():
        raise QualificationError("approved_by (a human approver) is required")
    expected = expected_confirmation_phrase(strategy.id)
    if confirmation_text != expected:
        raise QualificationError(f"confirmation text mismatch; expected exactly: {expected!r}")
    if acknowledged is not True:
        raise QualificationError("explicit acknowledgement (acknowledged=True) is required")

    moment = (as_of or datetime.now(IST)).astimezone(IST)
    return ManualApprovalRecord(
        strategy_id=strategy.id,
        approved_by=approved_by.strip(),
        confirmation_text=confirmation_text,
        previous_status=strategy.lifecycle_status,
        new_status=LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL,
        acknowledgement="Live eligibility is a governance label only; no broker order code is enabled by this approval.",
        approved_at_ist=moment.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def evaluation_to_markdown(evaluations: Sequence[TrialEvaluation], *, as_of: date | None = None) -> str:
    lines = [
        "# India Strategy Platform — One-Month Paper Qualification",
        "",
        "Paper/research only. No live orders. Live eligibility requires separate manual approval.",
        "",
    ]
    for ev in evaluations:
        verdict = "PASS" if ev.passed else "FAIL"
        lines.extend([
            f"## {ev.strategy_id} — {verdict}",
            f"- Trial window: {ev.window.start} → {ev.window.end} (exclusive)",
        ])
        if as_of is not None:
            lines.append(
                f"- Trial progress: {ev.window.progress_pct(as_of)}% "
                f"({'complete' if ev.window.is_complete(as_of) else 'in progress'})"
            )
        if ev.current_status is not None:
            lines.append(f"- Current status: `{ev.current_status.value}`")
        lines.append(f"- Recommended status: `{ev.recommended_status.value if ev.recommended_status else 'n/a'}`")
        lines.append(f"- {ev.recommendation_note}")
        lines.extend(["", "### Metrics", ""])
        m = ev.metrics
        lines.extend([
            f"- Closed trades: {m.closed_trades} (open: {m.open_trades}, trading days: {m.trading_days})",
            f"- Net P&L: ₹{m.net_pnl:.2f} · Expectancy/trade: ₹{m.expectancy:.2f}",
            f"- Win rate: {m.win_rate:.2f}% ({m.wins}W / {m.losses}L)",
            f"- Profit factor: {'inf' if m.profit_factor is None else f'{m.profit_factor:.2f}'}",
            f"- Avg win: ₹{m.avg_win:.2f} · Avg loss: ₹{m.avg_loss:.2f}",
            f"- Max drawdown: ₹{m.max_drawdown:.2f} · Max consecutive losses: {m.max_consecutive_losses}",
            "",
            "### Criteria",
            "",
            "| Criterion | Observed | Threshold | Result |",
            "| --- | --- | --- | --- |",
        ])
        for result in ev.results:
            lines.append(f"| {result.name} | {result.observed} | {result.threshold} | {'✅' if result.passed else '❌'} |")
        lines.append("")
    return "\n".join(lines)


def metrics_csv(evaluations: Sequence[TrialEvaluation]) -> str:
    buffer = io.StringIO()
    fieldnames = list(next(iter(evaluations)).metrics.as_display().keys()) + [
        "trial_start", "trial_end", "passed", "recommended_status",
    ] if evaluations else []
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for ev in evaluations:
        row = ev.metrics.as_display()
        row.update({
            "trial_start": str(ev.window.start),
            "trial_end": str(ev.window.end),
            "passed": str(ev.passed),
            "recommended_status": ev.recommended_status.value if ev.recommended_status else "",
        })
        writer.writerow(row)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Input loading (read-only)
# --------------------------------------------------------------------------- #
def load_trades_from_file(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".csv":
        return list(csv.DictReader(io.StringIO(text)))
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("trades", [])
    if not isinstance(data, list):
        raise QualificationError(f"{path}: expected a list of trade rows or an object with a 'trades' list")
    return data


SAMPLE_PASS_STRATEGY = "option_orb_debit_spread"
SAMPLE_FAIL_STRATEGY = "expiry_day_directional_defined_risk"


def sample_trades() -> list[dict[str, Any]]:
    """Deterministic sample paper trades for two strategies within a June-2026 trial.

    ``option_orb_debit_spread`` is engineered to PASS; ``expiry_day_directional_defined_risk``
    to FAIL (thin, net-negative) — so both branches are exercised without a DB. Both
    ids are real executable strategies in config/strategy_universe_india.json.
    """
    rows: list[dict[str, Any]] = []
    # Passing strategy: 18 closed trades, mostly small wins, positive net P&L.
    pass_pnls = [
        "1200", "-800", "1500", "900", "-1500", "1100", "1300", "-600", "1400",
        "800", "1000", "-900", "1250", "700", "-500", "1350", "950", "1150",
    ]
    for i, pnl in enumerate(pass_pnls):
        day = 1 + (i % 20)
        rows.append({
            "strategy_id": SAMPLE_PASS_STRATEGY,
            "status": "closed",
            "realized_pnl": pnl,
            "entry_time": f"2026-06-{day:02d}T09:45:00+05:30",
            "exit_time": f"2026-06-{day:02d}T11:30:00+05:30",
            "quantity": 30,
        })
    rows.append({
        "strategy_id": SAMPLE_PASS_STRATEGY, "status": "open", "realized_pnl": None,
        "entry_time": "2026-06-22T09:45:00+05:30", "exit_time": None, "quantity": 30,
    })
    # Failing strategy: few trades, net negative.
    fail_pnls = ["-1500", "600", "-1200", "-900", "500"]
    for i, pnl in enumerate(fail_pnls):
        day = 2 + i * 3
        rows.append({
            "strategy_id": SAMPLE_FAIL_STRATEGY,
            "status": "closed",
            "realized_pnl": pnl,
            "entry_time": f"2026-06-{day:02d}T13:00:00+05:30",
            "exit_time": f"2026-06-{day:02d}T15:10:00+05:30",
            "quantity": 25,
        })
    return rows


def strategy_ids_from_trades(trades: Sequence[PaperTrade]) -> list[str]:
    return sorted({t.strategy_id for t in trades})


def _status_for(strategy_id: str) -> LifecycleStatus | None:
    try:
        universe = load_registry()
    except Exception:
        return None
    strategy = universe.by_id(strategy_id)
    return strategy.lifecycle_status if strategy else None


def run_report(
    trades: Sequence[PaperTrade],
    *,
    window: TrialWindow,
    criteria: QualificationCriteria,
    strategy_ids: Sequence[str] | None,
    as_of: date,
) -> tuple[str, str]:
    ids = list(strategy_ids) if strategy_ids else strategy_ids_from_trades(trades)
    evaluations = [
        evaluate_trial(sid, trades, window=window, criteria=criteria, current_status=_status_for(sid))
        for sid in ids
    ]
    return evaluation_to_markdown(evaluations, as_of=as_of), metrics_csv(evaluations)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-month paper qualification (paper/research only)")
    parser.add_argument("--mode", choices=["sample", "file"], default="sample")
    parser.add_argument("--input", type=Path, help="JSON/CSV file of paper-trade rows (read-only)")
    parser.add_argument("--criteria", type=Path, help="JSON file of qualification thresholds (optional)")
    parser.add_argument("--strategy-id", action="append", dest="strategy_ids", help="restrict to strategy id(s)")
    parser.add_argument("--from", dest="from_date", help="trial start date YYYY-MM-DD (default: first trade's month start)")
    parser.add_argument("--as-of", dest="as_of", help="reference date YYYY-MM-DD for trial progress (default: today IST)")
    parser.add_argument("--out", type=Path, help="write the Markdown report to this path (CSV alongside)")
    # Manual approval (never automatic; enables no orders).
    parser.add_argument("--grant-live-eligibility", action="store_true")
    parser.add_argument("--approved-by")
    parser.add_argument("--confirm", dest="confirm")
    parser.add_argument("--acknowledge", action="store_true")
    return parser.parse_args(argv)


def _handle_grant(args: argparse.Namespace) -> int:
    if not args.strategy_ids or len(args.strategy_ids) != 1:
        print("--grant-live-eligibility requires exactly one --strategy-id")
        return 2
    strategy_id = args.strategy_ids[0]
    try:
        universe = load_registry()
    except Exception as exc:
        print(f"Cannot load registry: {exc}")
        return 1
    strategy = universe.by_id(strategy_id)
    if strategy is None:
        print(f"Unknown strategy id: {strategy_id}")
        return 1
    try:
        record = grant_live_eligibility(
            strategy,
            approved_by=args.approved_by or "",
            confirmation_text=args.confirm or "",
            acknowledged=bool(args.acknowledge),
        )
    except QualificationError as exc:
        print(f"Live eligibility NOT granted: {exc}")
        print(f"Hint: confirmation text must be exactly: {expected_confirmation_phrase(strategy_id)!r}")
        return 1
    print("\n".join([
        "## Manual Live-Eligibility Record",
        f"strategy: {record.strategy_id}",
        f"approved_by: {record.approved_by}",
        f"previous_status: {record.previous_status.value}",
        f"new_status: {record.new_status.value}",
        f"approved_at_ist: {record.approved_at_ist}",
        f"note: {record.acknowledgement}",
    ]))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.grant_live_eligibility:
        return _handle_grant(args)

    if args.mode == "file":
        if not args.input:
            print("--mode file requires --input")
            return 2
        raw = load_trades_from_file(args.input)
    else:
        raw = sample_trades()
    trades = normalize_trades(raw)
    if not trades:
        print("No trades to evaluate.")
        return 1

    criteria = QualificationCriteria.from_dict(
        json.loads(args.criteria.read_text(encoding="utf-8")) if args.criteria else None
    )

    if args.from_date:
        start = date.fromisoformat(args.from_date)
    else:
        first_exit = min((t.exit_time for t in trades if t.exit_time is not None), default=None)
        anchor = first_exit.date() if first_exit else datetime.now(IST).date()
        start = anchor.replace(day=1)
    window = TrialWindow.one_month_from(start)
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(IST).date()

    markdown, csv_text = run_report(
        trades, window=window, criteria=criteria, strategy_ids=args.strategy_ids, as_of=as_of
    )
    print(markdown)
    if args.out:
        args.out.write_text(markdown, encoding="utf-8")
        csv_path = args.out.with_suffix(".csv")
        csv_path.write_text(csv_text, encoding="utf-8")
        print(f"\nWrote {args.out} and {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
