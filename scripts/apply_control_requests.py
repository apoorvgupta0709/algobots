#!/usr/bin/env python3
"""Apply dashboard-submitted control requests. Paper-only control plane.

Reads pending rows from research.control_requests (inserted by the dashboard via
the insert-only dashboard_ctl role), validates them against whitelists and safe
bounds, and applies them:

- engine_pause / engine_resume -> research.control_state (engines skip new
  entries while paused but keep managing open positions).
- strategy_toggle / risk_cap_update -> atomic, revalidated edits to the engine
  config JSON, picked up on the engine's next cron invocation.
- force_exit is deliberately NOT handled here; the engines claim those requests
  themselves so the close uses their live quote/candle context.

Every config edit is re-validated through the owning engine's own load_config
before replacing the file, so paper_only / live_orders_enabled and all
cross-field risk rules can never be violated from the control plane.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

IST = ZoneInfo("Asia/Kolkata")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")

ENGINE_BANKNIFTY = "banknifty_options_paper"
ENGINE_PACK = "nse_intraday_options_strategy_pack"
ENGINES = (ENGINE_BANKNIFTY, ENGINE_PACK)

ENGINE_CONFIG_PATHS: dict[str, Path] = {
    ENGINE_BANKNIFTY: PROJECT_ROOT / "config" / "banknifty_options_paper.json",
    ENGINE_PACK: PROJECT_ROOT / "config" / "nse_intraday_options_strategy_pack.json",
}

# Safe bounds per engine for dashboard-editable risk caps. Single source of
# truth: the dashboard imports this to render input limits. Any key not listed
# here is rejected outright (paper_only / live_orders_enabled / force_exit_time
# are immutable from the control plane by construction).
RISK_CAP_BOUNDS: dict[str, dict[str, tuple[Decimal, Decimal]]] = {
    ENGINE_BANKNIFTY: {
        "max_daily_loss": (Decimal("500"), Decimal("10000")),
        "max_trade_loss": (Decimal("100"), Decimal("5000")),
        # The banknifty engine manages a single open position by design.
        "max_open_positions": (Decimal("1"), Decimal("1")),
        "max_trades_per_day": (Decimal("1"), Decimal("10")),
        "max_premium_exposure": (Decimal("1000"), Decimal("50000")),
    },
    ENGINE_PACK: {
        "max_daily_loss": (Decimal("500"), Decimal("10000")),
        # Pack validate() hard-caps max_trade_loss at 1500 and exposure at 40000.
        "max_trade_loss": (Decimal("100"), Decimal("1500")),
        "max_open_positions": (Decimal("1"), Decimal("3")),
        "max_trades_per_day": (Decimal("1"), Decimal("10")),
        "max_premium_exposure": (Decimal("1000"), Decimal("40000")),
    },
}

INT_CAP_KEYS = {"max_open_positions", "max_trades_per_day"}

# Top-level key -> documentation mirror key inside the banknifty "risk" block;
# the engine refuses to run when the two disagree, so edits must touch both.
BANKNIFTY_RISK_MIRROR = {
    "max_daily_loss": "max_daily_loss_inr",
    "max_trade_loss": "max_trade_loss_inr",
    "max_trades_per_day": "max_trades_per_day",
    "max_open_positions": "max_open_positions",
    "max_premium_exposure": "max_premium_exposure_inr",
}

# entry_function values that can never be armed from the dashboard.
NON_RUNNABLE_ENTRY_FUNCTIONS = {"", "not_implemented", "research_only", "risk_filter_not_implemented"}


class ControlRequestRejected(ValueError):
    """Raised by handlers; the message is recorded on the rejected request."""


def _decimal(value: Any, *, key: str) -> Decimal:
    if isinstance(value, bool):
        raise ControlRequestRejected(f"{key} must be a number, got a boolean")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ControlRequestRejected(f"{key} is not a valid number: {value!r}") from exc


def _strict_bool(value: Any, *, key: str) -> bool:
    if not isinstance(value, bool):
        raise ControlRequestRejected(f"{key} must be a JSON boolean, got {value!r}")
    return value


def coerce_like(existing: Any, value: Decimal) -> Any:
    """Write the new value with the same JSON type the config already uses.

    The pack config stores money as strings; the banknifty config stores
    numbers. Preserving the existing type keeps diffs minimal and parsers happy.
    """
    if isinstance(existing, str):
        return str(value.to_integral_value() if value == value.to_integral_value() else value)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def validate_cap_changes(engine: str, changes: Any) -> dict[str, Decimal]:
    if not isinstance(changes, dict) or not changes:
        raise ControlRequestRejected("risk_cap_update payload must include a non-empty 'changes' object")
    bounds = RISK_CAP_BOUNDS[engine]
    validated: dict[str, Decimal] = {}
    for key, raw in changes.items():
        if key not in bounds:
            raise ControlRequestRejected(
                f"risk cap '{key}' is not editable from the control plane "
                f"(editable: {', '.join(sorted(bounds))})"
            )
        value = _decimal(raw, key=key)
        if key in INT_CAP_KEYS and value != value.to_integral_value():
            raise ControlRequestRejected(f"{key} must be a whole number, got {raw!r}")
        low, high = bounds[key]
        if not (low <= value <= high):
            raise ControlRequestRejected(f"{key}={value} is outside the safe bounds [{low}, {high}]")
        validated[key] = value
    return validated


def apply_risk_caps_banknifty(data: dict[str, Any], changes: dict[str, Decimal]) -> dict[str, Any]:
    starting_capital = _decimal(data.get("starting_capital", "50000"), key="starting_capital")
    exposure = changes.get("max_premium_exposure")
    if exposure is not None and exposure > starting_capital:
        raise ControlRequestRejected(
            f"max_premium_exposure={exposure} exceeds starting_capital={starting_capital}"
        )
    risk_block = data.get("risk")
    for key, value in changes.items():
        data[key] = coerce_like(data.get(key), value)
        mirror = BANKNIFTY_RISK_MIRROR[key]
        if isinstance(risk_block, dict) and mirror in risk_block:
            risk_block[mirror] = coerce_like(risk_block.get(mirror), value)
    return data


def apply_risk_caps_pack(data: dict[str, Any], strategy_id: str, changes: dict[str, Decimal]) -> dict[str, Any]:
    strategies = data.get("strategies")
    if not isinstance(strategies, dict) or strategy_id not in strategies:
        raise ControlRequestRejected(f"unknown pack strategy_id {strategy_id!r}")
    strat = strategies[strategy_id]
    for key, value in changes.items():
        strat[key] = coerce_like(strat.get(key), value)
    return data


def apply_strategy_toggle_banknifty(data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    strategy_id = str(payload.get("strategy_id") or "").strip()
    router = data.get("strategy_router")
    if not isinstance(router, list):
        raise ControlRequestRejected("config has no strategy_router list")
    entry = next(
        (item for item in router if isinstance(item, dict) and (item.get("id") or item.get("strategy_id")) == strategy_id),
        None,
    )
    if entry is None:
        raise ControlRequestRejected(f"unknown banknifty strategy_id {strategy_id!r}")

    enabled = payload.get("enabled", entry.get("enabled"))
    paper_trade_enabled = payload.get("paper_trade_enabled", entry.get("paper_trade_enabled"))
    enabled = _strict_bool(enabled, key="enabled")
    paper_trade_enabled = _strict_bool(paper_trade_enabled, key="paper_trade_enabled")

    arming = (enabled and not entry.get("enabled")) or (paper_trade_enabled and not entry.get("paper_trade_enabled"))
    card_type = str(entry.get("card_type") or "unspecified").lower()
    entry_function = str(entry.get("entry_function") or "").strip()
    status = str(entry.get("status") or "").strip()
    if arming and (
        card_type != "entry"
        or entry_function in NON_RUNNABLE_ENTRY_FUNCTIONS
        or status in {"research_only", "research_only_blocked"}
    ):
        raise ControlRequestRejected(
            f"{strategy_id} cannot be armed from the dashboard: "
            f"card_type={card_type!r}, entry_function={entry_function!r}, status={status!r}. "
            "Research-only, filter, and guardrail cards stay disabled by design."
        )
    entry["enabled"] = enabled
    entry["paper_trade_enabled"] = paper_trade_enabled
    return data


def apply_strategy_toggle_pack(data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    strategy_id = str(payload.get("strategy_id") or "").strip()
    strategies = data.get("strategies")
    if not isinstance(strategies, dict) or strategy_id not in strategies:
        raise ControlRequestRejected(f"unknown pack strategy_id {strategy_id!r}")
    strat = strategies[strategy_id]
    strat["enabled"] = _strict_bool(payload.get("enabled", strat.get("enabled")), key="enabled")
    strat["paper_trade_enabled"] = _strict_bool(
        payload.get("paper_trade_enabled", strat.get("paper_trade_enabled")), key="paper_trade_enabled"
    )
    return data


def banknifty_config_validator(path: Path) -> None:
    from scripts.banknifty_options_paper import load_config

    try:
        load_config(path)
    except SystemExit as exc:  # the engine refuses unsafe configs via SystemExit
        raise ControlRequestRejected(f"engine validator refused the edit: {exc}") from exc
    except ValueError as exc:
        raise ControlRequestRejected(f"engine validator refused the edit: {exc}") from exc


def pack_config_validator(path: Path) -> None:
    from scripts.nse_intraday_options_strategy_pack import load_config

    try:
        load_config(path)
    except ValueError as exc:
        raise ControlRequestRejected(f"engine validator refused the edit: {exc}") from exc


CONFIG_VALIDATORS: dict[str, Callable[[Path], None]] = {
    ENGINE_BANKNIFTY: banknifty_config_validator,
    ENGINE_PACK: pack_config_validator,
}


def write_config_atomically(path: Path, data: dict[str, Any], validator: Callable[[Path], None]) -> None:
    """Temp-write -> revalidate via the engine's own loader -> backup -> replace."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}_ctl_", suffix=".json", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        validator(tmp_path)
        backup = path.with_name(f"{path.name}.bak_{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(path, backup)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def handle_config_request(engine: str, action_type: str, payload: dict[str, Any]) -> str:
    config_path = ENGINE_CONFIG_PATHS[engine]
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if action_type == "strategy_toggle":
        if engine == ENGINE_BANKNIFTY:
            data = apply_strategy_toggle_banknifty(data, payload)
        else:
            data = apply_strategy_toggle_pack(data, payload)
        summary = (
            f"set {payload.get('strategy_id')} enabled={payload.get('enabled')} "
            f"paper_trade_enabled={payload.get('paper_trade_enabled')}"
        )
    elif action_type == "risk_cap_update":
        changes = validate_cap_changes(engine, payload.get("changes"))
        if engine == ENGINE_BANKNIFTY:
            data = apply_risk_caps_banknifty(data, changes)
        else:
            strategy_id = str(payload.get("strategy_id") or "").strip()
            if not strategy_id:
                raise ControlRequestRejected("pack risk_cap_update payload must include strategy_id")
            data = apply_risk_caps_pack(data, strategy_id, changes)
        summary = "updated " + ", ".join(f"{k}={v}" for k, v in sorted(changes.items()))
    else:  # pragma: no cover - guarded by the dispatch in process_pending_requests
        raise ControlRequestRejected(f"unsupported action_type {action_type!r}")
    write_config_atomically(config_path, data, CONFIG_VALIDATORS[engine])
    return summary


def handle_pause_request(conn: psycopg.Connection, engine: str, action_type: str, requested_by: str, payload: dict[str, Any]) -> str:
    paused = action_type == "engine_pause"
    note = str(payload.get("note") or "")[:500] or None
    with conn.cursor() as cur:
        cur.execute(
            """
            update research.control_state
            set paused = %s,
                paused_at = case when %s then now() else null end,
                paused_by = case when %s then %s else null end,
                note = %s,
                updated_at = now()
            where engine = %s
            """,
            (paused, paused, paused, requested_by, note, engine),
        )
        if cur.rowcount != 1:
            raise ControlRequestRejected(f"no control_state row for engine {engine!r}")
    return "engine paused (new entries stop; open positions stay managed)" if paused else "engine resumed"


def expire_stale_requests(conn: psycopg.Connection) -> int:
    """Pending requests from a previous IST day must never fire later."""
    with conn.cursor() as cur:
        cur.execute(
            """
            update research.control_requests
            set status = 'expired', processed_at = now(),
                result_message = 'expired: requested before today''s session'
            where status = 'pending'
              and (requested_at at time zone 'Asia/Kolkata')::date
                  < (now() at time zone 'Asia/Kolkata')::date
            """
        )
        return cur.rowcount


def process_pending_requests(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Apply pending requests one at a time, oldest first, each in its own txn."""
    results: list[dict[str, Any]] = []
    while True:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select request_id, requested_by, engine, action_type, payload
                    from research.control_requests
                    where status = 'pending' and action_type <> 'force_exit'
                    order by requested_at, request_id
                    for update skip locked
                    limit 1
                    """
                )
                row = cur.fetchone()
                if row is None:
                    return results
                request_id, requested_by, engine, action_type, payload = row
                payload = payload if isinstance(payload, dict) else {}
                try:
                    if action_type in ("engine_pause", "engine_resume"):
                        message = handle_pause_request(conn, engine, action_type, requested_by, payload)
                    else:
                        message = handle_config_request(engine, action_type, payload)
                    status = "applied"
                except ControlRequestRejected as exc:
                    status, message = "rejected", str(exc)
                cur.execute(
                    """
                    update research.control_requests
                    set status = %s, processed_at = now(), result_message = %s
                    where request_id = %s
                    """,
                    (status, message[:1000], request_id),
                )
        results.append({"request_id": request_id, "engine": engine, "action": action_type, "status": status, "message": message})


def main() -> None:
    with psycopg.connect(DATABASE_URL, options="-c timezone=Asia/Kolkata") as conn:
        expired = expire_stale_requests(conn)
        conn.commit()
        results = process_pending_requests(conn)
    if not results and not expired:
        print("NO_CHANGE control requests")
        return
    if expired:
        print(f"EXPIRED {expired} stale pending request(s)")
    for result in results:
        print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
