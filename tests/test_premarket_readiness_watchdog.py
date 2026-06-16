from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.premarket_readiness_watchdog as wd
from scripts.premarket_readiness_watchdog import (
    DATA,
    REQUIRED,
    CheckResult,
    ReadinessReport,
    build_report,
    check_config_safety,
    check_constituent_coverage,
    check_credentials,
    check_ingest_freshness,
    check_underlying_quote,
    parse_as_of,
)

IST = wd.IST

# 09:25 IST on a trading day — inside the pre-entry window, before 09:35.
AS_OF = datetime(2026, 6, 16, 9, 25, 0, tzinfo=IST)
DEADLINE = dtime(9, 35, tzinfo=IST)
UNDERLYING = "NSE:NIFTYBANK-INDEX"


def _quote(seconds_old: int, *, ltp: float = 100.0, open_price: float = 100.0) -> dict:
    # ltp is only truth-checked (is None); open must be positive for the quote to
    # be usable for entries (the engine needs it to compute pct_from_open).
    return {
        "updated_at": AS_OF - timedelta(seconds=seconds_old),
        "quote_time": None,
        "ltp": ltp,
        "open": open_price,
    }


def _run(seconds_old: int, *, status: str = "success") -> dict:
    return {
        "status": status,
        "finished_at": AS_OF - timedelta(seconds=seconds_old),
        "started_at": AS_OF - timedelta(seconds=seconds_old + 5),
    }


def _raw(**overrides) -> dict:
    base = {
        "paper_only": True,
        "live_orders_enabled": False,
        "underlying_symbol": UNDERLYING,
        "quote_stale_seconds": 90,
        "min_constituent_coverage_pct": 70,
        "no_new_trades_before": "09:35",
        "constituents": [
            {"symbol": "HDFCBANK", "fyers_symbol": "NSE:HDFCBANK-EQ"},
            {"symbol": "ICICIBANK", "fyers_symbol": "NSE:ICICIBANK-EQ"},
            {"symbol": "SBIN", "fyers_symbol": "NSE:SBIN-EQ"},
            {"symbol": "AXISBANK", "fyers_symbol": "NSE:AXISBANK-EQ"},
        ],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# config safety
# --------------------------------------------------------------------------- #
def test_config_safety_pass():
    r = check_config_safety(_raw())
    assert r.ok and r.severity == REQUIRED


def test_config_safety_rejects_live_orders():
    r = check_config_safety(_raw(live_orders_enabled=True))
    assert not r.ok and r.severity == REQUIRED


def test_config_safety_rejects_paper_only_false():
    r = check_config_safety(_raw(paper_only=False))
    assert not r.ok


def test_config_safety_rejects_non_boolean():
    r = check_config_safety(_raw(paper_only="maybe"))
    assert not r.ok


# --------------------------------------------------------------------------- #
# credentials — presence only, never values
# --------------------------------------------------------------------------- #
def test_credentials_all_present():
    present = {k: True for k in wd.REQUIRED_CREDS + wd.ADVISORY_CREDS}
    r = check_credentials(present)
    assert r.ok


def test_credentials_missing_required_fails():
    present = {"FYERS_CLIENT_ID": True, "FYERS_ACCESS_TOKEN": False,
               "FYERS_SECRET_KEY": True, "FYERS_REDIRECT_URI": True}
    r = check_credentials(present)
    assert not r.ok
    assert "FYERS_ACCESS_TOKEN" in r.detail


def test_credentials_detail_has_no_values():
    present = {k: True for k in wd.REQUIRED_CREDS + wd.ADVISORY_CREDS}
    r = check_credentials(present)
    # Only names/presence words, never a secret value.
    assert "present" in r.detail


def test_credentials_missing_advisory_still_passes():
    present = {"FYERS_CLIENT_ID": True, "FYERS_ACCESS_TOKEN": True,
               "FYERS_SECRET_KEY": False, "FYERS_REDIRECT_URI": False}
    r = check_credentials(present)
    assert r.ok
    assert "advisory" in r.detail


# --------------------------------------------------------------------------- #
# quote-ingest freshness
# --------------------------------------------------------------------------- #
def test_ingest_fresh_passes():
    run = _run(120)
    assert check_ingest_freshness(run, run, AS_OF, 600).ok


def test_ingest_stale_fails():
    run = _run(900)
    r = check_ingest_freshness(run, run, AS_OF, 600)
    assert not r.ok and r.severity == DATA


def test_ingest_missing_fails():
    assert not check_ingest_freshness(None, None, AS_OF, 600).ok


def test_ingest_latest_failed_blocks_despite_recent_success():
    # The most recent quotes run FAILED (e.g. auth/token failure) but an older
    # success is still within the freshness window — must NOT report ready.
    latest = _run(60, status="failed")
    success = _run(300)  # well inside the 600s window
    r = check_ingest_freshness(latest, success, AS_OF, 600)
    assert not r.ok and r.severity == DATA
    assert "status=failed" in r.detail
    # The last good run is surfaced for context, but does not flip the result.
    assert "last success" in r.detail


def test_ingest_latest_running_blocks():
    latest = _run(30, status="running")
    r = check_ingest_freshness(latest, _run(300), AS_OF, 600)
    assert not r.ok and r.severity == DATA


# --------------------------------------------------------------------------- #
# underlying quote
# --------------------------------------------------------------------------- #
def test_underlying_fresh_passes():
    assert check_underlying_quote(UNDERLYING, _quote(30), AS_OF, 90).ok


def test_underlying_stale_fails():
    assert not check_underlying_quote(UNDERLYING, _quote(200), AS_OF, 90).ok


def test_underlying_missing_fails():
    assert not check_underlying_quote(UNDERLYING, None, AS_OF, 90).ok


def test_underlying_null_ltp_fails():
    row = {"updated_at": AS_OF, "quote_time": None, "ltp": None}
    assert not check_underlying_quote(UNDERLYING, row, AS_OF, 90).ok


def test_underlying_missing_open_fails():
    row = _quote(30)
    row.pop("open")
    r = check_underlying_quote(UNDERLYING, row, AS_OF, 90)
    assert not r.ok
    assert "positive open" in r.detail


def test_underlying_nonpositive_open_fails():
    r = check_underlying_quote(UNDERLYING, _quote(30, open_price=0), AS_OF, 90)
    assert not r.ok
    assert "positive open" in r.detail


# --------------------------------------------------------------------------- #
# constituent coverage
# --------------------------------------------------------------------------- #
def _constituents(raw):
    import scripts.banknifty_options_paper as bn
    return bn.parse_constituents(raw["constituents"])


def test_coverage_above_threshold_passes():
    raw = _raw()
    cons = _constituents(raw)
    rows = {c.fyers_symbol: _quote(30) for c in cons}  # 100% fresh
    assert check_constituent_coverage(cons, rows, AS_OF, 90, 70).ok


def test_coverage_below_threshold_fails():
    raw = _raw()
    cons = _constituents(raw)
    # only 1 of 4 fresh -> 25% < 70%
    rows = {cons[0].fyers_symbol: _quote(30)}
    r = check_constituent_coverage(cons, rows, AS_OF, 90, 70)
    assert not r.ok and r.severity == DATA
    assert "stale/missing" in r.detail


def test_coverage_counts_stale_as_missing():
    raw = _raw()
    cons = _constituents(raw)
    rows = {c.fyers_symbol: _quote(300) for c in cons}  # all stale
    assert not check_constituent_coverage(cons, rows, AS_OF, 90, 70).ok


def test_coverage_counts_missing_open_as_unusable():
    raw = _raw()
    cons = _constituents(raw)
    rows = {c.fyers_symbol: _quote(30, open_price=0) for c in cons}
    r = check_constituent_coverage(cons, rows, AS_OF, 90, 70)
    assert not r.ok and r.severity == DATA


# --------------------------------------------------------------------------- #
# report exit-code semantics
# --------------------------------------------------------------------------- #
def _checks(*, config=True, creds=True, data=True):
    out = [
        CheckResult("config_safety", "pass" if config else "fail", REQUIRED, ""),
        CheckResult("credentials", "pass" if creds else "fail", REQUIRED, ""),
        CheckResult("quote_ingest", "pass" if data else "fail", DATA, ""),
    ]
    return out


def test_report_all_pass_exit_zero():
    rep = ReadinessReport(AS_OF, DEADLINE, _checks(), strict=False)
    assert rep.ready and rep.exit_code == 0


def test_report_required_fail_exit_two():
    rep = ReadinessReport(AS_OF, DEADLINE, _checks(creds=False), strict=False)
    assert rep.exit_code == 2


def test_report_data_fail_before_deadline_exit_zero():
    # 09:25 < 09:35 -> still time, warn but exit 0
    rep = ReadinessReport(AS_OF, DEADLINE, _checks(data=False), strict=False)
    assert not rep.ready
    assert not rep.past_deadline
    assert rep.exit_code == 0


def test_report_data_fail_after_deadline_exit_one():
    after = datetime(2026, 6, 16, 9, 40, 0, tzinfo=IST)
    rep = ReadinessReport(after, DEADLINE, _checks(data=False), strict=False)
    assert rep.past_deadline
    assert rep.exit_code == 1


def test_report_strict_data_fail_before_deadline_exit_one():
    rep = ReadinessReport(AS_OF, DEADLINE, _checks(data=False), strict=True)
    assert rep.exit_code == 1


def test_report_required_fail_dominates_after_deadline():
    after = datetime(2026, 6, 16, 9, 40, 0, tzinfo=IST)
    rep = ReadinessReport(after, DEADLINE, _checks(config=False, data=False), strict=False)
    assert rep.exit_code == 2


# --------------------------------------------------------------------------- #
# build_report integration (no DB) + db error path
# --------------------------------------------------------------------------- #
def test_build_report_happy_path():
    raw = _raw()
    cons = _constituents(raw)
    quote_rows = {UNDERLYING: _quote(30)}
    quote_rows.update({c.fyers_symbol: _quote(30) for c in cons})
    rep = build_report(
        raw,
        as_of=AS_OF,
        alert_deadline=DEADLINE,
        max_quote_age_seconds=90,
        max_ingest_age_seconds=600,
        min_coverage_pct=70,
        strict=False,
        creds={k: True for k in wd.REQUIRED_CREDS + wd.ADVISORY_CREDS},
        latest_run=_run(60),
        latest_success=_run(60),
        quote_rows=quote_rows,
    )
    assert rep.ready and rep.exit_code == 0


def test_build_report_db_error_is_data_failure():
    rep = build_report(
        _raw(),
        as_of=AS_OF,
        alert_deadline=DEADLINE,
        max_quote_age_seconds=90,
        max_ingest_age_seconds=600,
        min_coverage_pct=70,
        strict=False,
        creds={k: True for k in wd.REQUIRED_CREDS + wd.ADVISORY_CREDS},
        db_error="connection refused",
    )
    assert not rep.ready
    db_check = next(c for c in rep.checks if c.name == "database")
    assert db_check.severity == DATA
    # before deadline -> exit 0; required checks (config/creds) still pass
    assert rep.exit_code == 0


def test_build_report_stale_ingest_after_deadline_exit_one():
    after = datetime(2026, 6, 16, 9, 40, 0, tzinfo=IST)
    raw = _raw()
    cons = _constituents(raw)
    quote_rows = {UNDERLYING: _quote(30)}
    quote_rows.update({c.fyers_symbol: _quote(30) for c in cons})
    rep = build_report(
        raw,
        as_of=after,
        alert_deadline=DEADLINE,
        max_quote_age_seconds=90,
        max_ingest_age_seconds=600,
        min_coverage_pct=70,
        strict=False,
        creds={k: True for k in wd.REQUIRED_CREDS + wd.ADVISORY_CREDS},
        latest_run={"status": "success", "finished_at": after - timedelta(seconds=5000), "started_at": None},
        latest_success={"status": "success", "finished_at": after - timedelta(seconds=5000), "started_at": None},
        quote_rows=quote_rows,
    )
    assert rep.exit_code == 1


# --------------------------------------------------------------------------- #
# as-of parsing
# --------------------------------------------------------------------------- #
def test_parse_as_of_naive_assumes_ist():
    parsed = parse_as_of("2026-06-16T09:25:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(hours=5, minutes=30)


def test_parse_as_of_respects_explicit_offset():
    parsed = parse_as_of("2026-06-16T09:25:00+05:30")
    assert parsed.hour == 9 and parsed.minute == 25
