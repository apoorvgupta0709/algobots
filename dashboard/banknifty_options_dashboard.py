#!/usr/bin/env python3
"""Read-only Streamlit dashboard for the BankNifty options paper monitor.

Safety design:
- No FYERS order calls.
- No LLM/API calls.
- Database access is SELECT-only and opens a read-only transaction.
- Cron/config files are read from local disk only.

Run with:
    uv run streamlit run dashboard/banknifty_options_dashboard.py \
      --server.address 127.0.0.1 --server.port 8501
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dependency exists in project, fallback for resilience
    load_dotenv = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = Path("/opt/data/profiles/finance")
CONFIG_PATH = PROJECT_ROOT / "config" / "banknifty_options_paper.json"
JOBS_PATH = PROFILE_ROOT / "cron" / "jobs.json"
DEFAULT_DB_PORT = "55432"
DEFAULT_DATABASE_URL = "postgresql://" + "dashboard_ro@" + "127.0.0.1" + ":" + DEFAULT_DB_PORT + "/finance_tracker"
MONITOR_JOB_NAME = "BankNifty options deterministic paper monitor: 5m entry / 15s open-trade monitor"
HEARTBEAT_JOB_NAME = "BankNifty options LLM heartbeat: cron/script safety audit"
DRIFT_GUARD_JOB_NAME = "BankNifty options script-only LLM drift guard"

@dataclass(frozen=True)
class SafetyCheck:
    name: str
    ok: bool
    detail: str


class DashboardError(RuntimeError):
    """Raised for dashboard data loading issues."""


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DashboardError(f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DashboardError(f"Invalid JSON in {path}: {exc}") from exc


def database_url() -> str:
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")
    # Prefer a least-privilege dashboard role; do not inherit the app/superuser DSN.
    return os.getenv("DASHBOARD_DATABASE_URL", DEFAULT_DATABASE_URL)


def connect_readonly() -> psycopg.Connection:
    conn = psycopg.connect(database_url(), row_factory=dict_row)
    # psycopg starts an implicit transaction on first execute. Set transaction
    # mode before any query so SHOW transaction_read_only is truly `on`.
    conn.read_only = True
    return conn


def assert_readonly_sql(sql: str) -> None:
    import re

    if "\x00" in sql:
        raise DashboardError("Dashboard SQL must not contain NUL bytes")
    stripped = sql.strip().lower()
    allowed = ("select", "with", "show")
    if not stripped.startswith(allowed):
        raise DashboardError("Dashboard SQL must be read-only SELECT/WITH/SHOW")
    # Defense-in-depth: reject write/DDL verbs and superuser-only filesystem
    # helpers as standalone SQL words even when separated by comments/whitespace.
    banned = (
        r"insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|"
        r"refresh\s+materialized\s+view|pg_read_file|pg_read_binary_file|"
        r"pg_ls_dir|lo_get"
    )
    if re.search(rf"\b({banned})\b", stripped):
        raise DashboardError("Dashboard SQL contains a banned write/DDL/superuser token")
    # The dashboard only needs one read-only statement per query. Blocking stacked
    # statements prevents `select ...; update ...` bypasses.
    statements = [part.strip() for part in stripped.split(";") if part.strip()]
    if len(statements) > 1:
        raise DashboardError("Dashboard SQL must contain exactly one read-only statement")


def fetch_rows(sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    assert_readonly_sql(sql)
    with connect_readonly() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params or ()))
            return list(cur.fetchall())


def find_job(jobs_doc: dict[str, Any], name: str) -> dict[str, Any] | None:
    for job in jobs_doc.get("jobs", []):
        if job.get("name") == name:
            return job
    return None


def schedule_expr(job: dict[str, Any] | None) -> str | None:
    if not job:
        return None
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        return schedule.get("expr") or schedule.get("display")
    if isinstance(schedule, str):
        return schedule
    return job.get("schedule_display")


def evaluate_system_safety(config: dict[str, Any], jobs_doc: dict[str, Any]) -> list[SafetyCheck]:
    monitor = find_job(jobs_doc, MONITOR_JOB_NAME)
    heartbeat = find_job(jobs_doc, HEARTBEAT_JOB_NAME)
    guard = find_job(jobs_doc, DRIFT_GUARD_JOB_NAME)
    risk_filter = config.get("risk_filter") or {}

    checks = [
        SafetyCheck("Paper-only config", config.get("paper_only") is True, f"paper_only={config.get('paper_only')!r}"),
        SafetyCheck("Live orders disabled", config.get("live_orders_enabled") is False, f"live_orders_enabled={config.get('live_orders_enabled')!r}"),
        SafetyCheck("Monitor job exists", monitor is not None, MONITOR_JOB_NAME),
        SafetyCheck("Monitor is script-only", bool(monitor and monitor.get("no_agent") is True), f"no_agent={None if monitor is None else monitor.get('no_agent')!r}"),
        SafetyCheck("Monitor has no model/provider", bool(monitor and not monitor.get("model") and not monitor.get("provider") and not monitor.get("base_url")), f"model/provider/base_url absent={bool(monitor and not monitor.get('model') and not monitor.get('provider') and not monitor.get('base_url'))}"),
        SafetyCheck("Monitor schedule", schedule_expr(monitor) == "* * * * 1-5", f"schedule={schedule_expr(monitor)!r}"),
        SafetyCheck("Entry cadence", config.get("entry_scan_interval_minutes") == 5, f"entry_scan_interval_minutes={config.get('entry_scan_interval_minutes')!r}"),
        SafetyCheck("Open-trade poll cadence", config.get("poll_interval_seconds") == 15, f"poll_interval_seconds={config.get('poll_interval_seconds')!r}"),
        SafetyCheck("30-minute LLM heartbeat", schedule_expr(heartbeat) == "0,30 4-10 * * 1-5", f"schedule={schedule_expr(heartbeat)!r}"),
        SafetyCheck("Script-only drift guard", bool(guard and guard.get("no_agent") is True), f"no_agent={None if guard is None else guard.get('no_agent')!r}"),
        SafetyCheck("Spread filter enabled", bool(risk_filter.get("enabled") is True and risk_filter.get("enforce_spread_filter") is True), f"risk_filter.enabled={risk_filter.get('enabled')!r}, enforce_spread_filter={risk_filter.get('enforce_spread_filter')!r}"),
    ]
    return checks


def inr(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        amount = Decimal(str(value))
    except Exception:
        return str(value)
    if not amount.is_finite():
        return str(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    return f"{sign}₹{amount:,.2f}"


def pct_from_open(ltp: Any, open_value: Any) -> Decimal | None:
    try:
        ltp_dec = Decimal(str(ltp))
        open_dec = Decimal(str(open_value))
    except Exception:
        return None
    if not ltp_dec.is_finite() or not open_dec.is_finite() or open_dec <= 0:
        return None
    return ((ltp_dec - open_dec) / open_dec * Decimal("100")).quantize(Decimal("0.01"))


def age_status(age_seconds: Any, stale_limit: int) -> tuple[str, str]:
    if age_seconds is None:
        return "UNKNOWN", "No quote timestamp"
    try:
        age = float(age_seconds)
    except Exception:
        return "UNKNOWN", str(age_seconds)
    if age < 0:
        return "UNKNOWN", f"clock skew: {age:.0f}s"
    if age <= stale_limit:
        return "OK", f"{age:.0f}s old"
    if age <= stale_limit * 2:
        return "WARN", f"{age:.0f}s old"
    return "STALE", f"{age:.0f}s old"


def get_db_snapshot() -> dict[str, list[dict[str, Any]]]:
    return {
        "campaign": fetch_rows(
            """
            select campaign_id, name, starting_capital, active, max_daily_loss,
                   max_open_positions, max_trades_per_day, stop_loss_pct, target_pct,
                   no_new_trades_after, force_exit_time, poll_interval_seconds, updated_at
            from research.option_paper_campaigns
            order by updated_at desc
            limit 1
            """
        ),
        "banknifty_quote": fetch_rows(
            """
            select symbol, ltp, open, high, low, close, volume, quote_time, updated_at,
                   extract(epoch from (now() - updated_at)) as age_seconds
            from market.quotes
            where symbol='NSE:NIFTYBANK-INDEX'
            order by updated_at desc
            limit 1
            """
        ),
        "open_trades": fetch_rows(
            """
            select t.option_trade_id, t.symbol, t.option_type, t.expiry, t.strike,
                   t.entry_premium, q.ltp as current_premium, t.stop_premium, t.target_premium,
                   t.highest_premium, t.quantity, t.premium_value, t.entry_time,
                   t.strategy_version,
                   case when q.ltp is not null then (q.ltp - t.entry_premium) * t.quantity else null end as unrealized_pnl,
                   extract(epoch from (now() - q.updated_at)) as quote_age_seconds
            from research.option_paper_trades t
            left join market.quotes q on q.symbol = t.symbol
            where t.status='open'
            order by t.entry_time desc
            """
        ),
        "risk_today": fetch_rows(
            """
            select count(*) filter (where created_at at time zone 'Asia/Kolkata' >= (now() at time zone 'Asia/Kolkata')::date) as trades_today,
                   coalesce(sum(case when status='closed' and exit_time at time zone 'Asia/Kolkata' >= (now() at time zone 'Asia/Kolkata')::date then realized_pnl else 0 end), 0) as realized_today,
                   count(*) filter (where status='open') as open_count,
                   count(*) as total_trades
            from research.option_paper_trades
            """
        ),
        "latest_events": fetch_rows(
            """
            select e.event_time, e.event_type, e.premium, e.quantity, e.message,
                   t.symbol, t.option_type
            from research.option_paper_trade_events e
            left join research.option_paper_trades t on t.option_trade_id = e.option_trade_id
            order by e.event_time desc
            limit 50
            """
        ),
        "daily_snapshots": fetch_rows(
            """
            select snapshot_date, starting_capital, realized_pnl, unrealized_pnl, equity,
                   open_positions, closed_positions
            from research.option_paper_daily_snapshots
            order by snapshot_date desc
            limit 30
            """
        ),
        "constituent_quotes": fetch_rows(
            """
            with configured as (
              select jsonb_array_elements(%s::jsonb) as item
            )
            select item->>'symbol' as symbol,
                   item->>'fyers_symbol' as fyers_symbol,
                   q.ltp, q.open, q.updated_at,
                   extract(epoch from (now() - q.updated_at)) as age_seconds,
                   case when q.open is not null and q.open > 0 then round(((q.ltp - q.open) / q.open * 100)::numeric, 2) else null end as pct_from_open
            from configured
            left join market.quotes q on q.symbol = item->>'fyers_symbol'
            order by pct_from_open desc nulls last
            """,
            [json.dumps(load_json_file(CONFIG_PATH).get("constituents") or [])],
        ),
    }


def render_status_badge(st: Any, check: SafetyCheck) -> None:
    if check.ok:
        st.success(f"{check.name}: OK — {check.detail}")
    else:
        st.error(f"{check.name}: FAIL — {check.detail}")


def main() -> None:  # pragma: no cover - exercised by Streamlit smoke import plus manual run
    import streamlit as st
    import streamlit.components.v1 as components

    st.set_page_config(page_title="BankNifty Options Paper Monitor", layout="wide")
    st.title("BankNifty Options Paper Monitor")
    st.caption("Read-only dashboard. No LLM calls. No FYERS order calls. PostgreSQL SELECT-only access.")

    with st.sidebar:
        st.header("Refresh")
        refresh_seconds = st.number_input("Auto-refresh seconds", min_value=0, max_value=300, value=15, step=5)
        if refresh_seconds:
            components.html(f"<script>setTimeout(() => window.parent.location.reload(), {int(refresh_seconds) * 1000});</script>", height=0)
        st.write(f"IST now: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}")
        st.write(f"Project: `{PROJECT_ROOT}`")
        st.write(f"DB: `127.0.0.1:55432/finance_tracker`")

    try:
        config = load_json_file(CONFIG_PATH)
        jobs_doc = load_json_file(JOBS_PATH)
        snapshot = get_db_snapshot()
    except Exception as exc:
        st.exception(exc)
        return

    safety_checks = evaluate_system_safety(config, jobs_doc)
    safe = all(check.ok for check in safety_checks)
    if safe:
        st.success("System safety checks passed: deterministic paper monitor is script-only and live orders are disabled.")
    else:
        st.error("System safety checks failed. Do not trust paper-monitor output until fixed.")

    tab_health, tab_live, tab_positions, tab_events, tab_equity, tab_cron = st.tabs([
        "System health", "Live market", "Open position", "Events", "Equity", "Cron/config"
    ])

    with tab_health:
        cols = st.columns(3)
        for idx, check in enumerate(safety_checks):
            with cols[idx % 3]:
                render_status_badge(st, check)

        campaign = snapshot["campaign"][0] if snapshot["campaign"] else {}
        risk = snapshot["risk_today"][0] if snapshot["risk_today"] else {}
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Starting capital", inr(campaign.get("starting_capital")))
        m2.metric("Realized today", inr(risk.get("realized_today")))
        m3.metric("Trades today", f"{risk.get('trades_today', 0)} / {config.get('max_trades_per_day')}")
        m4.metric("Open positions", f"{risk.get('open_count', 0)} / {config.get('max_open_positions')}")

    with tab_live:
        quote = snapshot["banknifty_quote"][0] if snapshot["banknifty_quote"] else {}
        age_label, age_detail = age_status(quote.get("age_seconds"), int(config.get("quote_stale_seconds", 90)))
        pct = pct_from_open(quote.get("ltp"), quote.get("open"))
        q1, q2, q3, q4 = st.columns(4)
        q1.metric("BankNifty LTP", inr(quote.get("ltp")))
        q2.metric("% from open", "n/a" if pct is None else f"{pct}%")
        q3.metric("Quote freshness", age_label, age_detail)
        q4.metric("Volume", str(quote.get("volume") or "n/a"))

        constituents = snapshot["constituent_quotes"]
        fresh = [row for row in constituents if row.get("age_seconds") is not None and float(row["age_seconds"]) <= float(config.get("quote_stale_seconds", 90))]
        coverage_pct = (len(fresh) / len(constituents) * 100) if constituents else 0
        st.metric("Fresh constituent coverage", f"{coverage_pct:.1f}%", f"{len(fresh)} / {len(constituents)}")
        st.subheader("Constituent movers")
        st.dataframe(constituents, use_container_width=True, hide_index=True)

    with tab_positions:
        open_trades = snapshot["open_trades"]
        if not open_trades:
            st.info("No open BankNifty option paper trades.")
        else:
            st.dataframe(open_trades, use_container_width=True, hide_index=True)

    with tab_events:
        st.dataframe(snapshot["latest_events"], use_container_width=True, hide_index=True)

    with tab_equity:
        daily = list(reversed(snapshot["daily_snapshots"]))
        if daily:
            st.line_chart({str(row["snapshot_date"]): float(row["equity"] or 0) for row in daily})
        st.dataframe(snapshot["daily_snapshots"], use_container_width=True, hide_index=True)

    with tab_cron:
        jobs = [find_job(jobs_doc, name) for name in [MONITOR_JOB_NAME, HEARTBEAT_JOB_NAME, DRIFT_GUARD_JOB_NAME]]
        compact_jobs = [
            {
                "id": job.get("id"),
                "name": job.get("name"),
                "enabled": job.get("enabled"),
                "state": job.get("state"),
                "schedule": schedule_expr(job),
                "script": job.get("script"),
                "no_agent": job.get("no_agent"),
                "model": job.get("model"),
                "provider": job.get("provider"),
                "last_run_at": job.get("last_run_at"),
                "last_status": job.get("last_status"),
                "next_run_at": job.get("next_run_at"),
            }
            for job in jobs if job
        ]
        st.dataframe(compact_jobs, use_container_width=True, hide_index=True)
        st.subheader("Runtime config")
        st.json({
            "campaign_name": config.get("campaign_name"),
            "strategy_version": config.get("strategy_version"),
            "paper_only": config.get("paper_only"),
            "live_orders_enabled": config.get("live_orders_enabled"),
            "entry_scan_interval_minutes": config.get("entry_scan_interval_minutes"),
            "poll_interval_seconds": config.get("poll_interval_seconds"),
            "max_trades_per_day": config.get("max_trades_per_day"),
            "max_daily_loss": config.get("max_daily_loss"),
            "max_trade_loss": config.get("max_trade_loss"),
            "risk_filter": config.get("risk_filter"),
        })


if __name__ == "__main__":
    main()
