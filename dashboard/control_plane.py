#!/usr/bin/env python3
"""Control-request submission for the dashboard. Paper-only by construction.

The dashboard's read path stays SELECT-only on the dashboard_ro role. This
module is the ONLY write path: one hardcoded, parameterized INSERT into
research.control_requests via the insert-only dashboard_ctl role (migration
015). Requests are validated and applied asynchronously by
scripts/apply_control_requests.py (or by the engines themselves for
force-exits); nothing here touches configs, engines, or any other table.

mode is always 'paper' — the live engine is a future phase, and the DB CHECK
constraint on control_requests.mode rejects anything else anyway.
"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Any

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTROL_DATABASE_URL = "postgresql://" + "dashboard_ctl@" + "127.0.0.1:55432" + "/finance_tracker"

ALLOWED_ENGINES = ("banknifty_options_paper", "nse_intraday_options_strategy_pack")
ALLOWED_ACTIONS = ("strategy_toggle", "force_exit", "engine_pause", "engine_resume", "risk_cap_update")
PIN_ENV_VAR = "DASHBOARD_CONTROL_PIN"
MAX_PIN_ATTEMPTS = 5


class ControlPlaneError(RuntimeError):
    """Raised for invalid control submissions before any SQL runs."""


def control_database_url() -> str:
    return os.getenv("CONTROL_DATABASE_URL", DEFAULT_CONTROL_DATABASE_URL)


def control_pin_configured() -> bool:
    return bool(os.getenv(PIN_ENV_VAR))


def verify_pin(entered: str) -> bool:
    """Constant-time PIN check against the env var. Fails closed when unset."""
    expected = os.getenv(PIN_ENV_VAR)
    if not expected or not entered:
        return False
    return hmac.compare_digest(entered.encode("utf-8"), expected.encode("utf-8"))


def submit_control_request(
    *,
    engine: str,
    action_type: str,
    payload: dict[str, Any],
    requested_by: str,
) -> int:
    """Insert exactly one pending control request; returns its request_id.

    Whitelist-validated here AND by DB CHECK constraints; payload values are
    bound parameters, never interpolated into SQL.
    """
    if engine not in ALLOWED_ENGINES:
        raise ControlPlaneError(f"engine {engine!r} is not controllable")
    if action_type not in ALLOWED_ACTIONS:
        raise ControlPlaneError(f"action_type {action_type!r} is not allowed")
    if not isinstance(payload, dict):
        raise ControlPlaneError("payload must be a dict")
    if not requested_by or not requested_by.strip():
        raise ControlPlaneError("requested_by is required")
    with psycopg.connect(control_database_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into research.control_requests (requested_by, engine, action_type, mode, payload)
                values (%s, %s, %s, 'paper', %s::jsonb)
                returning request_id
                """,
                (requested_by.strip()[:120], engine, action_type, json.dumps(payload)),
            )
            request_id = int(cur.fetchone()[0])
        conn.commit()
    return request_id
