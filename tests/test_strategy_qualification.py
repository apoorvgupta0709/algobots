from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.strategy_registry import LifecycleStatus, load_registry  # noqa: E402
from scripts.strategy_qualification import (  # noqa: E402
    IST,
    SAMPLE_FAIL_STRATEGY,
    SAMPLE_PASS_STRATEGY,
    QualificationError,
    TrialWindow,
    add_one_month,
    compute_metrics,
    evaluate_trial,
    evaluation_to_markdown,
    expected_confirmation_phrase,
    grant_live_eligibility,
    metrics_csv,
    normalize_trades,
    sample_trades,
)


def make_trades(strategy_id: str, pnls: list[int], *, start_day: int = 1) -> list:
    rows = []
    for i, pnl in enumerate(pnls):
        day = start_day + i
        rows.append({
            "strategy_id": strategy_id,
            "status": "closed",
            "realized_pnl": str(pnl),
            "entry_time": f"2026-06-{day:02d}T09:45:00+05:30",
            "exit_time": f"2026-06-{day:02d}T11:00:00+05:30",
            "quantity": 1,
        })
    return normalize_trades(rows)


JUNE_WINDOW = TrialWindow.one_month_from(date(2026, 6, 1))


# --------------------------------------------------------------------------- #
# Decimal-safe metrics.
# --------------------------------------------------------------------------- #
def test_compute_metrics_are_exact_and_decimal():
    trades = make_trades("s", [100, -50, 200, -30])
    m = compute_metrics("s", trades)
    assert m.closed_trades == 4
    assert (m.wins, m.losses) == (2, 2)
    assert m.gross_profit == Decimal("300.00")
    assert m.gross_loss == Decimal("80.00")
    assert m.net_pnl == Decimal("220.00")
    assert m.win_rate == Decimal("50.00")
    assert m.profit_factor == Decimal("3.75")
    assert m.expectancy == Decimal("55.00")
    assert m.avg_win == Decimal("150.00")
    assert m.avg_loss == Decimal("40.00")
    assert m.largest_win == Decimal("200.00")
    assert m.largest_loss == Decimal("-50.00")
    assert m.max_drawdown == Decimal("50.00")
    assert m.max_consecutive_losses == 1
    assert m.trading_days == 4
    for value in (m.net_pnl, m.gross_profit, m.gross_loss, m.win_rate,
                  m.expectancy, m.avg_win, m.avg_loss, m.max_drawdown, m.profit_factor):
        assert isinstance(value, Decimal)


def test_profit_factor_none_when_no_losses():
    m = compute_metrics("s", make_trades("s", [100, 200]))
    assert m.profit_factor is None
    assert m.gross_loss == Decimal("0.00")


def test_metrics_empty_is_zeroed():
    m = compute_metrics("nobody", [])
    assert m.closed_trades == 0
    assert m.net_pnl == Decimal("0")
    assert m.profit_factor is None


def test_consecutive_losses_and_drawdown():
    m = compute_metrics("s", make_trades("s", [-100, -200, -50, 400]))
    assert m.max_consecutive_losses == 3
    assert m.max_drawdown == Decimal("350.00")


# --------------------------------------------------------------------------- #
# Trial window helpers.
# --------------------------------------------------------------------------- #
def test_add_one_month_clamps_month_end():
    assert add_one_month(date(2026, 1, 31)) == date(2026, 2, 28)
    assert add_one_month(date(2026, 6, 1)) == date(2026, 7, 1)
    assert add_one_month(date(2026, 12, 15)) == date(2027, 1, 15)


def test_trial_window_contains_only_in_window_exits():
    assert JUNE_WINDOW.end == date(2026, 7, 1)
    in_trade = make_trades("s", [100], start_day=15)[0]
    out_trade = normalize_trades([{
        "strategy_id": "s", "status": "closed", "realized_pnl": "100",
        "entry_time": "2026-07-02T09:45:00+05:30", "exit_time": "2026-07-02T11:00:00+05:30",
    }])[0]
    assert JUNE_WINDOW.contains(in_trade.exit_time) is True
    assert JUNE_WINDOW.contains(out_trade.exit_time) is False


def test_evaluate_trial_excludes_out_of_window_trades():
    trades = make_trades("s", [1000] * 20, start_day=1)  # June, in window
    trades += normalize_trades([{
        "strategy_id": "s", "status": "closed", "realized_pnl": "999999",
        "entry_time": "2026-08-01T09:45:00+05:30", "exit_time": "2026-08-01T11:00:00+05:30",
    }])
    ev = evaluate_trial("s", trades, window=JUNE_WINDOW)
    assert ev.metrics.closed_trades == 20  # the August outlier is excluded


# --------------------------------------------------------------------------- #
# One-month trial pass/fail on the deterministic sample.
# --------------------------------------------------------------------------- #
def test_sample_pass_and_fail_strategies():
    trades = normalize_trades(sample_trades())
    ev_pass = evaluate_trial(SAMPLE_PASS_STRATEGY, trades, window=JUNE_WINDOW)
    ev_fail = evaluate_trial(SAMPLE_FAIL_STRATEGY, trades, window=JUNE_WINDOW)
    assert ev_pass.passed is True
    assert ev_fail.passed is False
    # The failing strategy must fail on trade count at minimum.
    failed = {r.name for r in ev_fail.results if not r.passed}
    assert "min_closed_trades" in failed


# --------------------------------------------------------------------------- #
# Recommendation never auto-promotes past QUALIFIED.
# --------------------------------------------------------------------------- #
def test_recommendation_ceiling_is_qualified():
    trades = normalize_trades(sample_trades())
    ev = evaluate_trial(SAMPLE_PASS_STRATEGY, trades, window=JUNE_WINDOW,
                        current_status=LifecycleStatus.PAPER_OBSERVING)
    assert ev.recommended_status is LifecycleStatus.QUALIFIED
    assert ev.recommended_status is not LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL


def test_passing_qualified_is_not_auto_promoted_to_live():
    trades = normalize_trades(sample_trades())
    ev = evaluate_trial(SAMPLE_PASS_STRATEGY, trades, window=JUNE_WINDOW,
                        current_status=LifecycleStatus.QUALIFIED)
    assert ev.recommended_status is LifecycleStatus.QUALIFIED


def test_failing_trial_holds_current_status():
    trades = normalize_trades(sample_trades())
    ev = evaluate_trial(SAMPLE_FAIL_STRATEGY, trades, window=JUNE_WINDOW,
                        current_status=LifecycleStatus.PAPER_ENABLED)
    assert ev.recommended_status is LifecycleStatus.PAPER_ENABLED


# --------------------------------------------------------------------------- #
# Manual live-eligibility approval — the only path, and it enables no orders.
# --------------------------------------------------------------------------- #
def test_grant_live_eligibility_success_is_label_only():
    universe = load_registry()
    strat = universe.by_id("sip_baseline_buy_and_hold")
    assert strat is not None and strat.lifecycle_status is LifecycleStatus.QUALIFIED
    record = grant_live_eligibility(
        strat,
        approved_by="Apoorv",
        confirmation_text=expected_confirmation_phrase(strat.id),
        acknowledged=True,
        as_of=datetime(2026, 7, 2, 10, 0, tzinfo=IST),
    )
    assert record.new_status is LifecycleStatus.LIVE_ELIGIBLE_REQUIRES_MANUAL_APPROVAL
    assert record.previous_status is LifecycleStatus.QUALIFIED
    assert "no broker order code" in record.acknowledgement.lower()


def test_grant_requires_qualified_status():
    universe = load_registry()
    strat = universe.by_id("option_orb_debit_spread")  # backtested, not qualified
    with pytest.raises(QualificationError):
        grant_live_eligibility(
            strat, approved_by="x",
            confirmation_text=expected_confirmation_phrase(strat.id), acknowledged=True,
        )


def test_grant_rejects_scorecard_strategy():
    universe = load_registry()
    strat = universe.by_id("iron_condor_scorecard")  # non-executable
    with pytest.raises(QualificationError):
        grant_live_eligibility(
            strat, approved_by="x",
            confirmation_text=expected_confirmation_phrase(strat.id), acknowledged=True,
        )


def test_grant_rejects_bad_confirmation_missing_approver_and_no_ack():
    universe = load_registry()
    strat = universe.by_id("sip_baseline_buy_and_hold")
    phrase = expected_confirmation_phrase(strat.id)
    with pytest.raises(QualificationError):
        grant_live_eligibility(strat, approved_by="x", confirmation_text="approve", acknowledged=True)
    with pytest.raises(QualificationError):
        grant_live_eligibility(strat, approved_by="", confirmation_text=phrase, acknowledged=True)
    with pytest.raises(QualificationError):
        grant_live_eligibility(strat, approved_by="x", confirmation_text=phrase, acknowledged=False)


# --------------------------------------------------------------------------- #
# Reports.
# --------------------------------------------------------------------------- #
def test_reports_render_markdown_and_csv():
    trades = normalize_trades(sample_trades())
    evs = [
        evaluate_trial(SAMPLE_PASS_STRATEGY, trades, window=JUNE_WINDOW),
        evaluate_trial(SAMPLE_FAIL_STRATEGY, trades, window=JUNE_WINDOW),
    ]
    md = evaluation_to_markdown(evs, as_of=date(2026, 7, 2))
    assert "One-Month Paper Qualification" in md
    assert SAMPLE_PASS_STRATEGY in md
    assert "PASS" in md and "FAIL" in md
    csv_text = metrics_csv(evs)
    header = csv_text.splitlines()[0]
    assert "strategy_id" in header and "recommended_status" in header
    assert SAMPLE_PASS_STRATEGY in csv_text
