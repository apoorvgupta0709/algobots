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
import sys
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
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dashboard import control_plane  # noqa: E402
from dashboard.strategy_cards import link_cards_to_strategies, load_strategy_cards  # noqa: E402
from scripts.apply_control_requests import INT_CAP_KEYS, RISK_CAP_BOUNDS  # noqa: E402

PROFILE_ROOT = Path("/opt/data/profiles/finance")
CONFIG_PATH = PROJECT_ROOT / "config" / "banknifty_options_paper.json"
PACK_CONFIG_PATH = PROJECT_ROOT / "config" / "nse_intraday_options_strategy_pack.json"
OPTIONS_CHAIN_CONFIG_PATH = PROJECT_ROOT / "config" / "options_chain.json"
JOBS_PATH = PROFILE_ROOT / "cron" / "jobs.json"
DEFAULT_DB_PORT = "55432"
DEFAULT_DATABASE_URL = "postgresql://" + "dashboard_ro@" + "127.0.0.1" + ":" + DEFAULT_DB_PORT + "/finance_tracker"
MONITOR_JOB_NAME = "BankNifty options deterministic paper monitor: 5m entry / 15s open-trade monitor"
HEARTBEAT_JOB_NAME = "BankNifty options LLM heartbeat: cron/script safety audit"
DRIFT_GUARD_JOB_NAME = "BankNifty options script-only LLM drift guard"
IST = ZoneInfo("Asia/Kolkata")

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


def load_json_file_or_empty(path: Path) -> dict[str, Any]:
    """Like load_json_file but tolerant of a missing/invalid optional config."""
    try:
        return load_json_file(path)
    except DashboardError:
        return {}


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


def fetch_rows_or_empty(sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    """fetch_rows but tolerant of tables from not-yet-applied migrations."""
    try:
        return fetch_rows(sql, params)
    except psycopg.errors.UndefinedTable:
        return []


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
        "chain_summary": fetch_rows(
            """
            select distinct on (underlying)
                   underlying, underlying_symbol, snapshot_time, expiry, spot, atm_strike,
                   total_ce_oi, total_pe_oi, pcr, max_pain_strike, atm_iv, iv_regime,
                   extract(epoch from (now() - snapshot_time)) as age_seconds
            from market.option_chain_summary
            order by underlying, snapshot_time desc
            """
        ),
        "chain_ladder": fetch_rows(
            """
            with latest as (
                select max(snapshot_time) as ts
                from market.option_chain_snapshots
                where underlying = %s
            )
            select s.strike,
                   max(case when s.option_type='CE' then s.oi end) as ce_oi,
                   max(case when s.option_type='CE' then s.iv end) as ce_iv,
                   max(case when s.option_type='CE' then s.delta end) as ce_delta,
                   max(case when s.option_type='CE' then s.ltp end) as ce_ltp,
                   max(case when s.option_type='PE' then s.ltp end) as pe_ltp,
                   max(case when s.option_type='PE' then s.delta end) as pe_delta,
                   max(case when s.option_type='PE' then s.iv end) as pe_iv,
                   max(case when s.option_type='PE' then s.oi end) as pe_oi
            from market.option_chain_snapshots s, latest
            where s.underlying = %s and s.snapshot_time = latest.ts
            group by s.strike
            order by s.strike
            """,
            ["BANKNIFTY", "BANKNIFTY"],
        ),
        # Migration 012 (pack) / 015 (control plane) tables; empty before those apply.
        "pack_open_trades": fetch_rows_or_empty(
            """
            select pack_trade_id, strategy_id, underlying, direction, structure,
                   entry_time, entry_underlying, stop_underlying, target_underlying, risk_rupees
            from research.strategy_pack_paper_trades
            where status='open'
            order by entry_time desc
            """
        ),
        "banknifty_trades_by_strategy": fetch_rows_or_empty(
            """
            select coalesce(raw->'strategy_card'->>'id', strategy_version) as strategy_id,
                   count(*) as trades,
                   count(*) filter (where status='open') as open_trades,
                   coalesce(sum(realized_pnl) filter (where status='closed'), 0) as realized_pnl,
                   max(entry_time) as last_entry
            from research.option_paper_trades
            group by 1
            """
        ),
        "pack_trades_by_strategy": fetch_rows_or_empty(
            """
            select strategy_id,
                   count(*) as trades,
                   count(*) filter (where status='open') as open_trades,
                   coalesce(sum(realized_pnl) filter (where status='closed'), 0) as realized_pnl,
                   max(entry_time) as last_entry
            from research.strategy_pack_paper_trades
            group by 1
            """
        ),
        "control_state": fetch_rows_or_empty(
            "select engine, paused, paused_at, paused_by, note, updated_at from research.control_state order by engine"
        ),
        "control_requests": fetch_rows_or_empty(
            """
            select request_id, requested_at, requested_by, engine, action_type, mode,
                   payload, status, processed_at, result_message
            from research.control_requests
            order by requested_at desc
            limit 30
            """
        ),
    }


def render_status_badge(st: Any, check: SafetyCheck) -> None:
    if check.ok:
        st.success(f"{check.name}: OK — {check.detail}")
    else:
        st.error(f"{check.name}: FAIL — {check.detail}")


def strategy_toggle_options(config: dict[str, Any], pack_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat list of every toggleable strategy with its current flags."""
    options: list[dict[str, Any]] = []
    for item in config.get("strategy_router") or []:
        if isinstance(item, dict) and (item.get("id") or item.get("strategy_id")):
            options.append({
                "engine": "banknifty_options_paper",
                "strategy_id": str(item.get("id") or item.get("strategy_id")),
                "name": str(item.get("name") or item.get("id")),
                "enabled": item.get("enabled") is True,
                "paper_trade_enabled": item.get("paper_trade_enabled") is True,
                "status": str(item.get("status") or ""),
            })
    for sid, strat in (pack_config.get("strategies") or {}).items():
        if isinstance(strat, dict):
            options.append({
                "engine": "nse_intraday_options_strategy_pack",
                "strategy_id": str(sid),
                "name": str(strat.get("name") or sid),
                "enabled": strat.get("enabled") is True,
                "paper_trade_enabled": strat.get("paper_trade_enabled") is True,
                "status": "",
            })
    return options


def render_strategies_tab(st: Any, snapshot: dict[str, Any], config: dict[str, Any], pack_config: dict[str, Any]) -> None:
    linked = link_cards_to_strategies(load_strategy_cards(), config, pack_config)
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for row in snapshot.get("banknifty_trades_by_strategy", []):
        stats[("banknifty_options_paper", str(row.get("strategy_id")))] = row
    for row in snapshot.get("pack_trades_by_strategy", []):
        stats[("nse_intraday_options_strategy_pack", str(row.get("strategy_id")))] = row

    def rank(item: Any) -> tuple[int, str]:
        if item.engine and item.enabled and item.paper_trade_enabled:
            return (0, item.card.title)
        if item.engine and item.enabled:
            return (1, item.card.title)
        if item.engine:
            return (2, item.card.title)
        return (3, item.card.title)

    st.caption(
        "Every strategy card, distilled: what it does, when it enters/exits, and its risk rules. "
        "ACTIVE = wired to an engine and allowed to open paper trades. Everything is paper-only."
    )
    for item in sorted(linked, key=rank):
        with st.container(border=True):
            head, badge = st.columns([4, 1])
            head.markdown(f"#### {item.card.title}")
            label = item.live_status_label
            if label == "ACTIVE (paper)":
                badge.success(label)
            elif item.engine is None:
                badge.info("research only")
            else:
                badge.warning(label)
            if item.card.what:
                st.markdown(f"**What it does:** {item.card.what}")
            col_entry, col_exit, col_risk = st.columns(3)
            for col, heading, bullets_list in (
                (col_entry, "Enters when", item.card.entry),
                (col_exit, "Exits when", item.card.exits),
                (col_risk, "Risk rules", item.card.risk),
            ):
                col.markdown(f"**{heading}**")
                for bullet in bullets_list or ["—"]:
                    col.markdown(f"- {bullet}")
            if item.card.filters:
                st.markdown("**Filters:** " + " · ".join(item.card.filters))
            if item.engine:
                row = stats.get((item.engine, item.strategy_id or ""))
                trades = row.get("trades", 0) if row else 0
                open_trades = row.get("open_trades", 0) if row else 0
                realized = inr(row.get("realized_pnl")) if row else inr(0)
                last_entry = row.get("last_entry") if row else None
                st.caption(
                    f"Engine: `{item.engine}` · id `{item.strategy_id}` · paper trades: {trades} "
                    f"({open_trades} open) · realized P&L: {realized} · last entry: {last_entry or 'never'}"
                )
            with st.expander("Full strategy card"):
                st.markdown(item.card.full_markdown)


def render_control_requests_table(st: Any, snapshot: dict[str, Any]) -> None:
    st.subheader("Recent control requests")
    rows = snapshot.get("control_requests", [])
    if not rows:
        st.info("No control requests yet.")
        return
    st.dataframe(
        [
            {
                "id": row.get("request_id"),
                "requested_at": row.get("requested_at"),
                "by": row.get("requested_by"),
                "engine": row.get("engine"),
                "action": row.get("action_type"),
                "payload": json.dumps(row.get("payload"), default=str),
                "status": row.get("status"),
                "result": row.get("result_message"),
            }
            for row in rows
        ],
        use_container_width=True,
        hide_index=True,
    )


def render_control_tab(st: Any, snapshot: dict[str, Any], config: dict[str, Any], pack_config: dict[str, Any]) -> None:
    st.subheader("Trading mode")
    mode = st.radio("Trading mode", ["Paper", "Live"], index=0, horizontal=True, label_visibility="collapsed")
    if mode == "Live":
        st.error(
            "Live mode is LOCKED. Live execution is a future, separately-gated phase "
            "(scripts/run_live_order_gate.py scaffolding). This dashboard only submits paper-mode "
            "control requests, and the database CHECK constraint on control_requests.mode rejects "
            "anything else. Switch back to Paper."
        )
        render_control_requests_table(st, snapshot)
        return
    st.caption(
        "Actions are queued in research.control_requests and applied asynchronously: "
        "toggles/caps/pause within ~1–2 minutes by the control applier; force-exits by the engines "
        "themselves (≤15 s for BankNifty while its tick loop runs, ≤1 min for the pack)."
    )

    if not control_plane.control_pin_configured():
        st.warning("Control actions are disabled: set DASHBOARD_CONTROL_PIN in the dashboard environment to enable them.")
        render_control_requests_table(st, snapshot)
        return

    if not st.session_state.get("control_unlocked"):
        attempts = int(st.session_state.get("pin_attempts", 0))
        if attempts >= control_plane.MAX_PIN_ATTEMPTS:
            st.error("Too many failed PIN attempts. Restart the dashboard session to retry.")
        else:
            with st.form("control_pin_form"):
                pin = st.text_input("Control PIN", type="password")
                if st.form_submit_button("Unlock controls"):
                    if control_plane.verify_pin(pin):
                        st.session_state["control_unlocked"] = True
                        st.session_state["pin_attempts"] = 0
                        st.rerun()
                    else:
                        st.session_state["pin_attempts"] = attempts + 1
                        st.error("Wrong PIN.")
        render_control_requests_table(st, snapshot)
        return

    requested_by = os.getenv("USER") or "dashboard"
    unlocked_bar, lock_col = st.columns([4, 1])
    unlocked_bar.success("Controls unlocked. Auto-refresh is paused while unlocked.")
    if lock_col.button("Lock controls"):
        st.session_state["control_unlocked"] = False
        st.rerun()

    def submit(engine: str, action_type: str, payload: dict[str, Any], note: str) -> None:
        try:
            request_id = control_plane.submit_control_request(
                engine=engine, action_type=action_type, payload=payload, requested_by=requested_by
            )
            st.success(f"Queued request #{request_id}: {note}")
        except Exception as exc:  # surfaced inline; nothing was applied
            st.error(f"Could not queue request: {exc}")

    # --- engine pause/resume -------------------------------------------------
    st.subheader("Engines")
    state_rows = {row.get("engine"): row for row in snapshot.get("control_state", [])}
    if not state_rows:
        st.info("Control tables not found — apply migrations/015_control_plane.sql first.")
    open_by_engine: dict[str, list[tuple[int, str]]] = {
        "banknifty_options_paper": [
            (int(row["option_trade_id"]), f"{row.get('symbol')} {row.get('option_type')} qty {row.get('quantity')} entry {inr(row.get('entry_premium'))}")
            for row in snapshot.get("open_trades", [])
        ],
        "nse_intraday_options_strategy_pack": [
            (int(row["pack_trade_id"]), f"{row.get('strategy_id')} {row.get('underlying')} {row.get('direction')} entry {row.get('entry_underlying')}")
            for row in snapshot.get("pack_open_trades", [])
        ],
    }
    for engine, row in state_rows.items():
        paused = row.get("paused") is True
        name_col, pause_col, resume_col, flatten_col = st.columns([3, 1, 1, 1])
        status_text = "⏸ PAUSED" + (f" by {row.get('paused_by')}" if row.get("paused_by") else "") if paused else "▶ running"
        name_col.markdown(f"**{engine}** — {status_text}")
        if pause_col.button("Pause", key=f"pause_{engine}", disabled=paused):
            submit(engine, "engine_pause", {"note": "paused from dashboard"}, f"pause {engine} (new entries stop; open positions stay managed)")
        if resume_col.button("Resume", key=f"resume_{engine}", disabled=not paused):
            submit(engine, "engine_resume", {}, f"resume {engine}")
        if flatten_col.button("Pause + flatten", key=f"flatten_{engine}"):
            submit(engine, "engine_pause", {"note": "pause+flatten from dashboard"}, f"pause {engine}")
            for trade_id, desc in open_by_engine.get(engine, []):
                submit(engine, "force_exit", {"trade_id": trade_id}, f"force-exit {desc}")
            if not open_by_engine.get(engine):
                st.info("No open positions to flatten for this engine.")
    st.caption("Pause stops new paper entries only — open positions continue to be managed (stops, targets, session force-exit).")

    # --- force-exit individual positions -------------------------------------
    st.subheader("Force-exit an open paper position")
    any_open = False
    for engine, rows in open_by_engine.items():
        for trade_id, desc in rows:
            any_open = True
            desc_col, confirm_col, button_col = st.columns([3, 1, 1])
            desc_col.write(f"`{engine}` · trade {trade_id}: {desc}")
            confirmed = confirm_col.checkbox("confirm", key=f"fx_confirm_{engine}_{trade_id}")
            if button_col.button("Force exit", key=f"fx_{engine}_{trade_id}", disabled=not confirmed):
                submit(engine, "force_exit", {"trade_id": trade_id}, f"force-exit trade {trade_id}")
    if not any_open:
        st.info("No open paper positions.")

    # --- strategy enable/disable ----------------------------------------------
    st.subheader("Strategy enable/disable")
    options = strategy_toggle_options(config, pack_config)
    if options:
        labels = [f"{opt['name']}  ({opt['engine']})" for opt in options]
        choice = st.selectbox("Strategy", labels, key="toggle_strategy")
        selected = options[labels.index(choice)]
        with st.form("strategy_toggle_form"):
            enabled = st.checkbox("enabled", value=selected["enabled"])
            paper_enabled = st.checkbox("paper_trade_enabled", value=selected["paper_trade_enabled"])
            if st.form_submit_button("Queue toggle"):
                submit(
                    selected["engine"],
                    "strategy_toggle",
                    {"strategy_id": selected["strategy_id"], "enabled": enabled, "paper_trade_enabled": paper_enabled},
                    f"set {selected['strategy_id']} enabled={enabled} paper_trade_enabled={paper_enabled}",
                )
        st.caption("Research-only / filter / guardrail cards are rejected by the applier when armed; disabling is always allowed.")

    # --- risk caps --------------------------------------------------------------
    st.subheader("Risk caps")
    cap_engine = st.selectbox("Engine", list(control_plane.ALLOWED_ENGINES), key="caps_engine")
    cap_strategy: str | None = None
    if cap_engine == "nse_intraday_options_strategy_pack":
        pack_ids = list((pack_config.get("strategies") or {}).keys())
        cap_strategy = st.selectbox("Pack strategy", pack_ids, key="caps_strategy") if pack_ids else None
    bounds = RISK_CAP_BOUNDS[cap_engine]
    cap_key = st.selectbox("Cap", sorted(bounds), key="caps_key")
    low, high = int(bounds[cap_key][0]), int(bounds[cap_key][1])
    if cap_engine == "banknifty_options_paper":
        current_raw = config.get(cap_key)
    else:
        current_raw = ((pack_config.get("strategies") or {}).get(cap_strategy or "") or {}).get(cap_key)
    try:
        current_value = max(low, min(high, int(Decimal(str(current_raw)))))
    except Exception:
        current_value = low
    step = 1 if cap_key in INT_CAP_KEYS else 100
    new_value = st.number_input(
        f"{cap_key} (allowed {low}–{high}; current {current_raw})",
        min_value=low, max_value=high, value=current_value, step=step, key="caps_value",
    )
    if st.button("Queue cap update"):
        payload: dict[str, Any] = {"changes": {cap_key: int(new_value)}}
        if cap_strategy:
            payload["strategy_id"] = cap_strategy
        submit(cap_engine, "risk_cap_update", payload, f"set {cap_key}={int(new_value)}")

    render_control_requests_table(st, snapshot)


def main() -> None:  # pragma: no cover - exercised by Streamlit smoke import plus manual run
    import streamlit as st
    import streamlit.components.v1 as components

    st.set_page_config(page_title="BankNifty Options Paper Monitor", layout="wide")
    st.title("BankNifty Options Paper Monitor")
    st.caption("Read-only dashboard. No LLM calls. No FYERS order calls. PostgreSQL SELECT-only access.")

    with st.sidebar:
        st.header("Refresh")
        refresh_seconds = st.number_input("Auto-refresh seconds", min_value=0, max_value=300, value=15, step=5)
        # The reload wipes session state (and with it the control-PIN unlock),
        # so auto-refresh is suspended while the Control tab is unlocked.
        if refresh_seconds and not st.session_state.get("control_unlocked"):
            components.html(f"<script>setTimeout(() => window.parent.location.reload(), {int(refresh_seconds) * 1000});</script>", height=0)
        elif refresh_seconds:
            st.caption("Auto-refresh paused while controls are unlocked.")
        st.write(f"IST now: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}")
        st.write(f"Project: `{PROJECT_ROOT}`")
        st.write(f"DB: `127.0.0.1:55432/finance_tracker`")

    try:
        config = load_json_file(CONFIG_PATH)
        pack_config = load_json_file_or_empty(PACK_CONFIG_PATH)
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

    tab_health, tab_live, tab_chain, tab_positions, tab_strategies, tab_control, tab_events, tab_equity, tab_cron = st.tabs([
        "System health", "Live market", "Options chain", "Open position", "Strategies", "Control", "Events", "Equity", "Cron/config"
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

    with tab_chain:
        chain_stale = int(load_json_file_or_empty(OPTIONS_CHAIN_CONFIG_PATH).get("snapshot_stale_seconds", 180))
        summary_rows = snapshot["chain_summary"]
        if not summary_rows:
            st.info("No option-chain snapshots yet. Run scripts/ingest_fyers_optionchain.py during market hours.")
        else:
            st.subheader("Chain summary")
            for srow in summary_rows:
                fresh_label, fresh_detail = age_status(srow.get("age_seconds"), chain_stale)
                st.markdown(f"**{srow.get('underlying')}** — expiry {srow.get('expiry')} · {fresh_label} ({fresh_detail})")
                c1, c2, c3, c4, c5, c6 = st.columns(6)
                c1.metric("Spot", inr(srow.get("spot")))
                c2.metric("ATM strike", str(srow.get("atm_strike") or "n/a"))
                c3.metric("PCR (PE/CE OI)", str(srow.get("pcr") or "n/a"))
                c4.metric("Max pain", str(srow.get("max_pain_strike") or "n/a"))
                c5.metric("ATM IV", str(srow.get("atm_iv") or "n/a"))
                c6.metric("IV regime", str(srow.get("iv_regime") or "n/a"))

            ladder = snapshot["chain_ladder"]
            st.subheader("BankNifty strike ladder (latest snapshot)")
            if not ladder:
                st.info("No BankNifty ladder rows in the latest snapshot.")
            else:
                st.caption("CE OI / IV / Δ  |  strike  |  PE OI / IV / Δ")
                st.dataframe(ladder, use_container_width=True, hide_index=True)

    with tab_positions:
        open_trades = snapshot["open_trades"]
        if not open_trades:
            st.info("No open BankNifty option paper trades.")
        else:
            st.dataframe(open_trades, use_container_width=True, hide_index=True)

    with tab_strategies:
        render_strategies_tab(st, snapshot, config, pack_config)

    with tab_control:
        render_control_tab(st, snapshot, config, pack_config)

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
