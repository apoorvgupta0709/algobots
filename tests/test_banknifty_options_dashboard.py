from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess

import sys

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "banknifty_options_dashboard.py"
spec = importlib.util.spec_from_file_location("banknifty_options_dashboard", MODULE_PATH)
dashboard = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["banknifty_options_dashboard"] = dashboard
spec.loader.exec_module(dashboard)


def job(name, *, no_agent=True, schedule="* * * * 1-5", script="x.sh", model=None, provider=None, base_url=None):
    return {
        "name": name,
        "enabled": True,
        "no_agent": no_agent,
        "script": script,
        "model": model,
        "provider": provider,
        "base_url": base_url,
        "schedule": {"expr": schedule},
    }


def safe_config():
    return {
        "paper_only": True,
        "live_orders_enabled": False,
        "entry_scan_interval_minutes": 5,
        "poll_interval_seconds": 15,
        "risk_filter": {"enabled": True, "enforce_spread_filter": True},
    }


def safe_jobs():
    return {
        "jobs": [
            job(dashboard.MONITOR_JOB_NAME, no_agent=True, schedule="* * * * 1-5"),
            job(dashboard.HEARTBEAT_JOB_NAME, no_agent=None, schedule="0,30 4-10 * * 1-5"),
            job(dashboard.DRIFT_GUARD_JOB_NAME, no_agent=True, schedule="*/5 4-10 * * 1-5"),
        ]
    }


def test_evaluate_system_safety_all_ok_for_current_design():
    checks = dashboard.evaluate_system_safety(safe_config(), safe_jobs())
    assert checks
    assert all(check.ok for check in checks)


@pytest.mark.parametrize(
    "mutator, failing_name",
    [
        (lambda cfg, jobs: cfg.update({"paper_only": False}), "Paper-only config"),
        (lambda cfg, jobs: cfg.update({"live_orders_enabled": True}), "Live orders disabled"),
        (lambda cfg, jobs: jobs["jobs"][0].update({"no_agent": False}), "Monitor is script-only"),
        (lambda cfg, jobs: jobs["jobs"][0].update({"model": {"model": "expensive-model"}}), "Monitor has no model/provider"),
        (lambda cfg, jobs: jobs["jobs"][1]["schedule"].update({"expr": "* * * * 1-5"}), "30-minute LLM heartbeat"),
    ],
)
def test_evaluate_system_safety_detects_drift(mutator, failing_name):
    cfg = safe_config()
    jobs = safe_jobs()
    mutator(cfg, jobs)
    checks = dashboard.evaluate_system_safety(cfg, jobs)
    failed = {check.name for check in checks if not check.ok}
    assert failing_name in failed


def test_assert_readonly_sql_rejects_writes():
    dashboard.assert_readonly_sql("select 1")
    dashboard.assert_readonly_sql("with x as (select 1) select * from x")
    with pytest.raises(dashboard.DashboardError):
        dashboard.assert_readonly_sql("update research.option_paper_trades set status='x'")
    with pytest.raises(dashboard.DashboardError):
        dashboard.assert_readonly_sql("select 1; delete from research.option_paper_trades")
    with pytest.raises(dashboard.DashboardError):
        dashboard.assert_readonly_sql("select 1;\nupdate research.option_paper_trades set status='x'")
    with pytest.raises(dashboard.DashboardError):
        dashboard.assert_readonly_sql("select 1;\tdelete from research.option_paper_trades")


def test_assert_readonly_sql_rejects_superuser_file_read_and_nul_tricks():
    with pytest.raises(dashboard.DashboardError):
        dashboard.assert_readonly_sql("select pg_read_file('/etc/passwd')")
    with pytest.raises(dashboard.DashboardError):
        dashboard.assert_readonly_sql("select pg_ls_dir('.')")
    with pytest.raises(dashboard.DashboardError):
        dashboard.assert_readonly_sql("select\x00 1")


def test_formatting_helpers():
    assert dashboard.inr(1234.5) == "₹1,234.50"
    assert dashboard.inr(-10) == "-₹10.00"
    assert dashboard.inr(dashboard.Decimal("NaN")) == "NaN"
    assert dashboard.inr(dashboard.Decimal("Infinity")) == "Infinity"
    assert dashboard.pct_from_open(110, 100) == dashboard.Decimal("10.00")
    assert dashboard.pct_from_open("Infinity", 100) is None
    assert dashboard.pct_from_open(110, "NaN") is None
    assert dashboard.age_status(30, 90)[0] == "OK"
    assert dashboard.age_status(-1, 90)[0] == "UNKNOWN"
    assert dashboard.age_status(100, 90)[0] == "WARN"
    assert dashboard.age_status(300, 90)[0] == "STALE"


def test_database_url_prefers_dashboard_readonly_url(monkeypatch):
    monkeypatch.setattr(dashboard, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("DATABASE_URL", "postgres://primary-db")
    monkeypatch.setenv("DASHBOARD_DATABASE_URL", "postgres://readonly-db")

    assert dashboard.database_url() == "postgres://readonly-db"


def test_dashboard_runner_refuses_external_bind_without_explicit_ack():
    runner = Path(__file__).resolve().parents[1] / "scripts" / "run_banknifty_options_dashboard.sh"
    result = subprocess.run(
        ["bash", str(runner)],
        cwd=runner.parents[1],
        env={"BANKNIFTY_DASHBOARD_HOST": "0.0.0.0", "BANKNIFTY_DASHBOARD_VALIDATE_ONLY": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "refusing external dashboard bind" in result.stdout.lower()


# --- control plane: PIN + submission validation --------------------------------

import importlib.util as _ilu

_CP_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "control_plane.py"
_cp_spec = _ilu.spec_from_file_location("dashboard_control_plane", _CP_PATH)
control_plane = _ilu.module_from_spec(_cp_spec)
assert _cp_spec and _cp_spec.loader
sys.modules["dashboard_control_plane"] = control_plane
_cp_spec.loader.exec_module(control_plane)


def test_read_path_still_rejects_writes_after_control_plane() -> None:
    for sql in (
        "insert into research.control_requests (requested_by) values ('x')",
        "update research.control_state set paused=true",
        "select 1; insert into research.control_requests values (1)",
    ):
        with pytest.raises(dashboard.DashboardError):
            dashboard.assert_readonly_sql(sql)


def test_submit_control_request_rejects_bad_inputs_before_any_sql() -> None:
    with pytest.raises(control_plane.ControlPlaneError):
        control_plane.submit_control_request(
            engine="not_an_engine", action_type="engine_pause", payload={}, requested_by="t"
        )
    with pytest.raises(control_plane.ControlPlaneError):
        control_plane.submit_control_request(
            engine="banknifty_options_paper", action_type="drop_table", payload={}, requested_by="t"
        )
    with pytest.raises(control_plane.ControlPlaneError):
        control_plane.submit_control_request(
            engine="banknifty_options_paper", action_type="engine_pause", payload=[], requested_by="t"  # type: ignore[arg-type]
        )
    with pytest.raises(control_plane.ControlPlaneError):
        control_plane.submit_control_request(
            engine="banknifty_options_paper", action_type="engine_pause", payload={}, requested_by="  "
        )


def test_verify_pin_fails_closed_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv(control_plane.PIN_ENV_VAR, raising=False)
    assert control_plane.control_pin_configured() is False
    assert control_plane.verify_pin("anything") is False
    assert control_plane.verify_pin("") is False


def test_verify_pin_constant_time_match(monkeypatch) -> None:
    monkeypatch.setenv(control_plane.PIN_ENV_VAR, "secret-pin")
    assert control_plane.control_pin_configured() is True
    assert control_plane.verify_pin("secret-pin") is True
    assert control_plane.verify_pin("wrong") is False
    assert control_plane.verify_pin("") is False


def test_strategy_toggle_options_lists_both_engines() -> None:
    config = {"strategy_router": [{"id": "s1", "name": "S1", "enabled": True, "paper_trade_enabled": True}]}
    pack = {"strategies": {"p1": {"name": "P1", "enabled": False, "paper_trade_enabled": False}}}
    options = dashboard.strategy_toggle_options(config, pack)
    assert [(o["engine"], o["strategy_id"], o["enabled"]) for o in options] == [
        ("banknifty_options_paper", "s1", True),
        ("nse_intraday_options_strategy_pack", "p1", False),
    ]
