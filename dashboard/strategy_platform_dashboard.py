#!/usr/bin/env python3
"""Read-only Streamlit dashboard for the India strategy platform.

Three desks — consolidated **Options Desk**, **Equities Desk**, **Investment Desk** —
showing each strategy's lifecycle status, one-month paper-trial metrics/progress, a
platform safety panel, and per-strategy explainability (what it does / enters /
exits / filters / rationale).

Safety design (identical stance to the BankNifty monitor):
- No FYERS/broker order calls. No LLM/network calls.
- Database access is SELECT-only via a read-only transaction; if the DB is
  unavailable the dashboard falls back to bundled sample data and says so.
- Binds to loopback by default.

Run with:
    uv run streamlit run dashboard/strategy_platform_dashboard.py \
      --server.address 127.0.0.1 --server.port 8502
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.strategy_registry import (  # noqa: E402
    Desk,
    DeskInfo,
    LifecycleStatus,
    RegistryError,
    StrategyDefinition,
    StrategyUniverse,
    load_registry,
)
from scripts.strategy_qualification import (  # noqa: E402
    PaperTrade,
    QualificationCriteria,
    QualificationMetrics,
    TrialEvaluation,
    TrialWindow,
    compute_metrics,
    evaluate_trial,
    normalize_trades,
    sample_trades,
)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_DB_PORT = "55432"
# Prefer the least-privilege read-only dashboard role; never the app/superuser DSN.
# Deliberately omit passwords from the fallback URL; deployments should provide
# STRATEGY_DASHBOARD_DATABASE_URL or DASHBOARD_DATABASE_URL for a least-privilege
# read-only role.
DEFAULT_DATABASE_URL = f"postgresql://dashboard_ro@127.0.0.1:{DEFAULT_DB_PORT}/finance_tracker"

DB_SOURCE = "PostgreSQL research.option_paper_trades"
SAMPLE_SOURCE = "bundled sample data"


class DashboardError(RuntimeError):
    """Raised when the read-only dashboard rejects unsafe input."""


@dataclass(frozen=True)
class SafetyCheck:
    name: str
    ok: bool
    detail: str


# --------------------------------------------------------------------------- #
# Read-only SQL guard (defense-in-depth, mirrors the BankNifty dashboard)
# --------------------------------------------------------------------------- #
def assert_readonly_sql(sql: str) -> None:
    import re

    if "\x00" in sql:
        raise DashboardError("Dashboard SQL must not contain NUL bytes")
    stripped = sql.strip().lower()
    if not stripped.startswith(("select", "with", "show")):
        raise DashboardError("Dashboard SQL must be read-only SELECT/WITH/SHOW")
    banned = (
        r"insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|"
        r"refresh\s+materialized\s+view|pg_read_file|pg_read_binary_file|pg_ls_dir|lo_get"
    )
    if re.search(rf"\b({banned})\b", stripped):
        raise DashboardError("Dashboard SQL contains a banned write/DDL/superuser token")
    statements = [part.strip() for part in stripped.split(";") if part.strip()]
    if len(statements) > 1:
        raise DashboardError("Dashboard SQL must contain exactly one read-only statement")


def database_url() -> str:
    # Read only from the process environment; this dashboard never reads the .env
    # file. Prefer an explicit read-only DSN, else the least-privilege loopback role.
    return os.getenv("STRATEGY_DASHBOARD_DATABASE_URL", os.getenv("DASHBOARD_DATABASE_URL", DEFAULT_DATABASE_URL))


def fetch_rows(sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    """Execute one read-only statement. Import psycopg lazily so tests need no DB."""
    assert_readonly_sql(sql)
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(database_url(), row_factory=dict_row)
    conn.read_only = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))
            return list(cur.fetchall())
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Safe adapters — real registry / DB when available, sample data otherwise
# --------------------------------------------------------------------------- #
def sample_universe() -> StrategyUniverse:
    """Minimal three-desk universe used when the JSON registry is unavailable."""
    def strat(**kwargs: Any) -> StrategyDefinition:
        base: dict[str, Any] = dict(
            filters=(), rationale="", data_requirements=(), tags=(), risk=None,
            paper_only=True, live_orders_enabled=False,
        )
        base.update(kwargs)
        return StrategyDefinition(**base)

    from scripts.strategy_registry import Direction, Instrument, Structure, StrategyRisk, Timeframe

    demo_risk = StrategyRisk(Decimal("50000"), Decimal("1500"), Decimal("5000"), Decimal("40000"), 3, 1)
    strategies = (
        strat(id="option_orb_debit_spread", name="Index Option ORB Debit Spread", desk=Desk.OPTIONS,
              family="option_orb", instrument=Instrument.INDEX_OPTION, timeframe=Timeframe.INTRADAY,
              direction=Direction.DIRECTIONAL, structure=Structure.DEBIT_SPREAD, executable=True,
              option_selling=False, lifecycle_status=LifecycleStatus.PAPER_OBSERVING,
              description="Opening-range breakout expressed as a defined-risk debit spread.",
              entry="Break of the first 15m range with volume.", exit="Target/stop or 15:20 force exit.",
              risk=demo_risk),
        strat(id="short_straddle_scorecard", name="Short Straddle Scorecard", desk=Desk.OPTIONS,
              family="straddle", instrument=Instrument.INDEX_OPTION, timeframe=Timeframe.INTRADAY,
              direction=Direction.NONE, structure=Structure.STRADDLE, executable=False,
              option_selling=True, lifecycle_status=LifecycleStatus.RESEARCH_CANDIDATE,
              description="Short-premium straddle — scorecard only, never executed.",
              entry="n/a (scorecard only).", exit="n/a (scorecard only)."),
        strat(id="orb_retest_equity", name="ORB Retest (Equity)", desk=Desk.EQUITIES,
              family="orb", instrument=Instrument.EQUITY, timeframe=Timeframe.INTRADAY,
              direction=Direction.LONG, structure=Structure.SINGLE_LEG, executable=True,
              option_selling=False, lifecycle_status=LifecycleStatus.PAPER_ENABLED,
              description="Long the retest of a confirmed opening-range breakout.",
              entry="Retest of breakout level holds.", exit="Stop below retest / target R-multiple.",
              risk=demo_risk),
        strat(id="dma_momentum_50_200", name="50/200 DMA Momentum", desk=Desk.INVESTMENT,
              family="momentum", instrument=Instrument.EQUITY, timeframe=Timeframe.LONG_TERM,
              direction=Direction.LONG, structure=Structure.NONE, executable=True,
              option_selling=False, lifecycle_status=LifecycleStatus.BACKTESTED,
              description="Golden-cross long-only positional momentum.",
              entry="50 DMA crosses above 200 DMA.", exit="50 DMA crosses below 200 DMA.",
              risk=demo_risk),
        strat(id="risk_parity_allocation", name="Risk Parity Allocation", desk=Desk.INVESTMENT,
              family="risk_parity", instrument=Instrument.PORTFOLIO, timeframe=Timeframe.LONG_TERM,
              direction=Direction.NONE, structure=Structure.PORTFOLIO, executable=False,
              option_selling=False, lifecycle_status=LifecycleStatus.RESEARCH_CANDIDATE,
              description="Inverse-volatility weighted multi-asset allocation study.",
              entry="n/a (allocation study).", exit="n/a (allocation study)."),
    )
    desks = {
        Desk.OPTIONS: DeskInfo(Desk.OPTIONS, "Options Desk", "Defined-risk index-option strategies; short premium is scorecard-only."),
        Desk.EQUITIES: DeskInfo(Desk.EQUITIES, "Equities Desk", "Intraday/swing cash-equity long strategies."),
        Desk.INVESTMENT: DeskInfo(Desk.INVESTMENT, "Investment Desk", "Positional / long-term allocation and momentum studies."),
    }
    return StrategyUniverse(
        schema_version="sample", paper_only=True, live_orders_enabled=False,
        notes="Bundled sample universe (registry JSON unavailable).", desks=desks, strategies=strategies,
    )


def load_universe(path: Path | None = None) -> tuple[StrategyUniverse, str]:
    """Load the real registry; fall back to the bundled sample if it is missing/invalid."""
    try:
        universe = load_registry(path) if path else load_registry()
        return universe, "config/strategy_universe_india.json"
    except RegistryError:
        return sample_universe(), SAMPLE_SOURCE


def load_paper_trades() -> tuple[list[PaperTrade], str]:
    """Read closed/open paper trades read-only; fall back to sample trades on any error."""
    sql = """
        select coalesce(raw->'strategy_card'->>'id', strategy_version) as strategy_id,
               status, realized_pnl, entry_time, exit_time, quantity
        from research.option_paper_trades
    """
    try:
        rows = fetch_rows(sql)
        if not rows:
            return normalize_trades(sample_trades()), SAMPLE_SOURCE
        return normalize_trades(rows), DB_SOURCE
    except Exception:
        # Undefined table, no DB, or missing driver — degrade to sample data.
        return normalize_trades(sample_trades()), SAMPLE_SOURCE


def infer_trial_window(trades: Iterable[PaperTrade]) -> TrialWindow:
    exits = [t.exit_time for t in trades if t.exit_time is not None]
    anchor = min(exits).date() if exits else datetime.now(IST).date()
    return TrialWindow.one_month_from(anchor.replace(day=1))


# --------------------------------------------------------------------------- #
# Safety panel
# --------------------------------------------------------------------------- #
def evaluate_platform_safety(universe: StrategyUniverse) -> list[SafetyCheck]:
    executable_short = [s for s in universe.strategies if s.executable and s.option_selling]
    executable_live = [s for s in universe.strategies if s.executable and s.live_orders_enabled]
    non_paper = [s for s in universe.strategies if s.paper_only is not True]
    from scripts.strategy_registry import SHORT_PREMIUM_STRUCTURES
    executable_undefined = [s for s in universe.strategies if s.executable and s.structure in SHORT_PREMIUM_STRUCTURES]
    live_labelled = [s for s in universe.strategies if s.is_live_eligible]

    return [
        SafetyCheck("Registry is paper-only", universe.paper_only is True, f"paper_only={universe.paper_only!r}"),
        SafetyCheck("Live orders disabled", universe.live_orders_enabled is False, f"live_orders_enabled={universe.live_orders_enabled!r}"),
        SafetyCheck("Every strategy paper-only", not non_paper, f"non-paper strategies: {[s.id for s in non_paper]}"),
        SafetyCheck("No executable short-premium", not executable_short, f"offending: {[s.id for s in executable_short]}"),
        SafetyCheck("No executable undefined-risk structures", not executable_undefined, f"offending: {[s.id for s in executable_undefined]}"),
        SafetyCheck("No strategy enables live orders", not executable_live, f"offending: {[s.id for s in executable_live]}"),
        SafetyCheck("Live eligibility is manual-only", all(s.paper_only and not s.live_orders_enabled for s in live_labelled),
                    f"{len(live_labelled)} live-eligible label(s); all still paper-only"),
        SafetyCheck("DB access is read-only adapter", True, "assert_readonly_sql guards every query; connection set read_only"),
    ]


# --------------------------------------------------------------------------- #
# View models (pure, testable)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StrategyView:
    strategy: StrategyDefinition
    metrics: QualificationMetrics | None
    evaluation: TrialEvaluation | None

    @property
    def status_label(self) -> str:
        if not self.strategy.executable:
            return "SCORECARD ONLY"
        return self.strategy.lifecycle_status.value.replace("_", " ").upper()

    @property
    def trial_state(self) -> str:
        if not self.strategy.executable or self.evaluation is None:
            return "n/a"
        if self.metrics is None or self.metrics.closed_trades == 0:
            return "no paper trades yet"
        return "PASS" if self.evaluation.passed else "FAIL"


def build_desk_view(
    universe: StrategyUniverse,
    trades: Iterable[PaperTrade],
    *,
    criteria: QualificationCriteria | None = None,
    window: TrialWindow | None = None,
) -> dict[Desk, list[StrategyView]]:
    """Assemble per-desk strategy views with metrics for executable strategies."""
    trades = list(trades)
    window = window or infer_trial_window(trades)
    criteria = criteria or QualificationCriteria()
    view: dict[Desk, list[StrategyView]] = {desk: [] for desk in universe.desks}
    for strategy in universe.strategies:
        metrics: QualificationMetrics | None = None
        evaluation: TrialEvaluation | None = None
        if strategy.executable:
            metrics = compute_metrics(strategy.id, trades)
            evaluation = evaluate_trial(
                strategy.id, trades, window=window, criteria=criteria,
                current_status=strategy.lifecycle_status,
            )
        view.setdefault(strategy.desk, []).append(StrategyView(strategy, metrics, evaluation))
    return view


def inr(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        amount = Decimal(str(value))
    except Exception:
        return str(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{abs(amount):,.2f}"


# --------------------------------------------------------------------------- #
# Streamlit UI (not unit-tested; exercised by import + manual run)
# --------------------------------------------------------------------------- #
def _render_explainability(st: Any, strategy: StrategyDefinition) -> None:  # pragma: no cover - UI
    col_a, col_b = st.columns(2)
    col_a.markdown(f"**Enters when:** {strategy.entry or '—'}")
    col_b.markdown(f"**Exits when:** {strategy.exit or '—'}")
    if strategy.filters:
        st.markdown("**Filters:** " + " · ".join(strategy.filters))
    if strategy.rationale:
        st.caption(f"Why: {strategy.rationale}")
    meta = f"desk `{strategy.desk.value}` · family `{strategy.family}` · {strategy.instrument.value} · {strategy.timeframe.value} · {strategy.direction.value}/{strategy.structure.value}"
    st.caption(meta)


def _render_metrics(st: Any, view: StrategyView) -> None:  # pragma: no cover - UI
    m = view.metrics
    if m is None:
        st.info("Scorecard-only strategy — no paper P&L is generated (by design).")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Net P&L", inr(m.net_pnl))
    c2.metric("Win rate", f"{m.win_rate:.1f}%")
    c3.metric("Profit factor", "inf" if m.profit_factor is None else f"{m.profit_factor:.2f}")
    c4.metric("Closed trades", str(m.closed_trades))
    if view.evaluation is not None and m.closed_trades:
        passed = view.evaluation.passed
        (st.success if passed else st.warning)(
            f"One-month trial: {'PASS' if passed else 'FAIL'} — {view.evaluation.recommendation_note}"
        )


def main() -> None:  # pragma: no cover - exercised by Streamlit smoke import + manual run
    import streamlit as st

    st.set_page_config(page_title="India Strategy Platform", layout="wide")
    st.title("India Strategy Platform — Paper Research Monitor")
    st.caption("Read-only. No LLM calls. No broker/FYERS order calls. PostgreSQL SELECT-only, sample fallback.")

    universe, uni_source = load_universe()
    trades, trades_source = load_paper_trades()
    window = infer_trial_window(trades)
    desk_view = build_desk_view(universe, trades, window=window)

    with st.sidebar:
        st.header("Sources")
        st.write(f"Registry: `{uni_source}`")
        st.write(f"Paper trades: `{trades_source}`")
        st.write(f"Trial window: {window.start} → {window.end}")
        st.write(f"IST now: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}")
        st.write(f"DB: `127.0.0.1:{DEFAULT_DB_PORT}/finance_tracker` (read-only)")

    checks = evaluate_platform_safety(universe)
    if all(c.ok for c in checks):
        st.success("Platform safety checks passed: paper-only, no live orders, short premium is scorecard-only.")
    else:
        st.error("Platform safety checks FAILED — do not trust the platform until resolved.")

    tab_safety, tab_options, tab_equities, tab_investment = st.tabs(
        ["Safety", "Options Desk", "Equities Desk", "Investment Desk"]
    )

    with tab_safety:
        cols = st.columns(2)
        for i, check in enumerate(checks):
            with cols[i % 2]:
                (st.success if check.ok else st.error)(f"{check.name}: {'OK' if check.ok else 'FAIL'} — {check.detail}")

    for tab, desk in ((tab_options, Desk.OPTIONS), (tab_equities, Desk.EQUITIES), (tab_investment, Desk.INVESTMENT)):
        with tab:
            info = universe.desks.get(desk)
            if info:
                st.subheader(info.name)
                st.caption(info.description)
            views = sorted(desk_view.get(desk, []), key=lambda v: (not v.strategy.executable, v.strategy.name))
            if not views:
                st.info("No strategies on this desk.")
            for view in views:
                with st.container(border=True):
                    head, badge = st.columns([4, 1])
                    head.markdown(f"#### {view.strategy.name}")
                    if not view.strategy.executable:
                        badge.info(view.status_label)
                    elif view.trial_state == "PASS":
                        badge.success(view.status_label)
                    else:
                        badge.warning(view.status_label)
                    st.markdown(f"**What it does:** {view.strategy.description}")
                    _render_explainability(st, view.strategy)
                    _render_metrics(st, view)


if __name__ == "__main__":
    main()
