"""Thin httpx wrapper for control actions against the API service.

The dashboard never writes to the DB directly — promotions, kill switch and
gate re-evaluation all go through the API (env ``ALGOBOT_API_URL``, default
``http://localhost:8000``). Every call returns ``(ok, payload)``; connection
errors return ``(False, {"error": ...})`` so pages can show "API offline"
instead of crashing.
"""
from __future__ import annotations

from typing import Any

import httpx

from algobot.core.config import env

TIMEOUT = 5.0


def _base_url() -> str:
    return (env("ALGOBOT_API_URL", "http://localhost:8000") or "").rstrip("/")


def _call(method: str, path: str, json: dict | None = None) -> tuple[bool, dict[str, Any]]:
    url = _base_url() + path
    try:
        resp = httpx.request(method, url, json=json, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return False, {"error": f"API offline ({exc.__class__.__name__}): {exc}", "url": url}
    try:
        payload = resp.json()
    except ValueError:
        payload = {"detail": resp.text}
    if not isinstance(payload, dict):
        payload = {"data": payload}
    if not resp.is_success and "error" not in payload:
        payload["error"] = f"HTTP {resp.status_code}"
    return resp.is_success, payload


def get_status() -> tuple[bool, dict[str, Any]]:
    """Engine/API health snapshot."""
    return _call("GET", "/status")


def promote(strategy_id: str, target_mode: str, force: bool = False) -> tuple[bool, dict[str, Any]]:
    return _call("POST", f"/strategies/{strategy_id}/promote",
                 json={"target_mode": target_mode, "force": force})


def demote(strategy_id: str, target_mode: str) -> tuple[bool, dict[str, Any]]:
    return _call("POST", f"/strategies/{strategy_id}/demote",
                 json={"target_mode": target_mode})


def killswitch(on: bool, reason: str) -> tuple[bool, dict[str, Any]]:
    return _call("POST", "/risk/killswitch", json={"on": on, "reason": reason})


def evaluate_gates() -> tuple[bool, dict[str, Any]]:
    return _call("POST", "/gates/evaluate")
