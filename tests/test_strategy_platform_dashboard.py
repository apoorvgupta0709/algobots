from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard import strategy_platform_dashboard as dash  # noqa: E402
from scripts.strategy_registry import (  # noqa: E402
    Desk,
    DeskInfo,
    Direction,
    Instrument,
    LifecycleStatus,
    Structure,
    StrategyDefinition,
    StrategyUniverse,
    Timeframe,
)
from scripts.strategy_qualification import TrialWindow, normalize_trades, sample_trades  # noqa: E402


# --------------------------------------------------------------------------- #
# Read-only SQL guard.
# --------------------------------------------------------------------------- #
def test_assert_readonly_sql_accepts_reads():
    dash.assert_readonly_sql("select 1")
    dash.assert_readonly_sql("with x as (select 1) select * from x")


def test_assert_readonly_sql_rejects_writes_and_tricks():
    bad = [
        "update t set x=1",
        "delete from t",
        "insert into t values (1)",
        "drop table t",
        "select 1; delete from t",
        "select 1;\nupdate t set x=1",
        "select pg_read_file('/etc/passwd')",
    ]
    for sql in bad:
        with pytest.raises(dash.DashboardError):
            dash.assert_readonly_sql(sql)
    with pytest.raises(dash.DashboardError):
        dash.assert_readonly_sql("select 1\x00")


# --------------------------------------------------------------------------- #
# Universe loading + safety panel.
# --------------------------------------------------------------------------- #
def test_load_universe_real_is_safe():
    universe, source = dash.load_universe()
    assert source.endswith("strategy_universe_india.json")
    assert universe.paper_only is True
    assert all(check.ok for check in dash.evaluate_platform_safety(universe))


def test_load_universe_falls_back_to_sample_when_missing():
    universe, source = dash.load_universe(Path("/nonexistent/does-not-exist.json"))
    assert source == dash.SAMPLE_SOURCE
    assert universe.schema_version == "sample"
    # The bundled fallback must itself be safe.
    assert all(check.ok for check in dash.evaluate_platform_safety(universe))


def test_safety_panel_flags_executable_short_premium():
    unsafe = StrategyDefinition(
        id="bad", name="Bad", desk=Desk.OPTIONS, family="straddle",
        instrument=Instrument.INDEX_OPTION, timeframe=Timeframe.INTRADAY,
        direction=Direction.NONE, structure=Structure.STRADDLE, executable=True,
        option_selling=True, lifecycle_status=LifecycleStatus.RESEARCH_CANDIDATE,
        paper_only=True, live_orders_enabled=False, description="", entry="", exit="",
    )
    universe = StrategyUniverse(
        "x", True, False, "", {Desk.OPTIONS: DeskInfo(Desk.OPTIONS, "Options", "")}, (unsafe,)
    )
    failed = {c.name for c in dash.evaluate_platform_safety(universe) if not c.ok}
    assert "No executable short-premium" in failed
    assert "No executable undefined-risk structures" in failed


# --------------------------------------------------------------------------- #
# Desk view assembly.
# --------------------------------------------------------------------------- #
def test_build_desk_view_groups_and_metrics():
    universe, _ = dash.load_universe()
    trades = normalize_trades(sample_trades())
    window = TrialWindow.one_month_from(date(2026, 6, 1))
    view = dash.build_desk_view(universe, trades, window=window)
    assert {Desk.OPTIONS, Desk.EQUITIES, Desk.INVESTMENT, Desk.FUTURES} <= set(view.keys())
    for views in view.values():
        for sv in views:
            if sv.strategy.executable:
                assert sv.metrics is not None
                assert sv.evaluation is not None
            else:
                assert sv.metrics is None
                assert sv.status_label == "SCORECARD ONLY"
    pass_view = next(
        sv for views in view.values() for sv in views
        if sv.strategy.id == "option_orb_debit_spread"
    )
    assert pass_view.trial_state == "PASS"


# --------------------------------------------------------------------------- #
# Safe data adapter — DB when available, sample otherwise. No live DB in tests.
# --------------------------------------------------------------------------- #
def test_load_paper_trades_falls_back_to_sample(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("no database available")

    monkeypatch.setattr(dash, "fetch_rows", boom)
    trades, source = dash.load_paper_trades()
    assert source == dash.SAMPLE_SOURCE
    assert trades


def test_load_paper_trades_uses_db_rows_when_present(monkeypatch):
    rows = [{
        "strategy_id": "option_orb_debit_spread", "status": "closed", "realized_pnl": "100",
        "entry_time": "2026-06-01T09:45:00+05:30", "exit_time": "2026-06-01T11:00:00+05:30",
        "quantity": 1,
    }]
    monkeypatch.setattr(dash, "fetch_rows", lambda *a, **k: rows)
    trades, source = dash.load_paper_trades()
    assert source == dash.DB_SOURCE
    assert trades[0].strategy_id == "option_orb_debit_spread"


def test_inr_formatting():
    assert dash.inr(Decimal("1500")) == "₹1,500.00"
    assert dash.inr(Decimal("-1500.5")) == "-₹1,500.50"
    assert dash.inr(None) == "n/a"
