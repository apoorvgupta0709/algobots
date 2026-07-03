"""Control routes: promote/demote strategies, kill switch, gate evaluation.

The engine modules (``algobot.engine.lifecycle`` / ``algobot.engine.gate``)
belong to a parallel workstream and are imported LAZILY inside the action
helpers; when they are not importable yet the API degrades to a 503 with a
hint instead of failing to boot.

The ``do_*`` helpers are the single implementation of each action and are
reused verbatim by the query worker (promote/demote/killswitch/evaluate_gates
job types).
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from algobot.api import readers
from algobot.core.enums import Mode
from algobot.core.exceptions import GateError

log = logging.getLogger(__name__)

router = APIRouter(tags=["control"])


# --------------------------------------------------------------------------- errors
class UnknownStrategyError(Exception):
    """Strategy id not found in the DB nor in the plugin registry."""


class EngineUnavailableError(Exception):
    """A required engine module is not importable yet (parallel workstream)."""

    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------------- shared action helpers
def do_set_mode(strategy_id: str, target_mode: str, force: bool = False,
                actor: str = "api") -> dict:
    """Change a strategy's lifecycle mode via engine.lifecycle.set_mode.

    Raises UnknownStrategyError (unknown id — checked against DB+registry
    BEFORE the lazy engine import), EngineUnavailableError (module missing),
    GateError (promotion blocked by the paper-to-live gate).
    """
    if not readers.strategy_exists(strategy_id):
        raise UnknownStrategyError(f"unknown strategy '{strategy_id}'")
    try:
        from algobot.engine import lifecycle
    except ImportError as e:
        raise EngineUnavailableError(
            f"engine lifecycle module unavailable: {e}",
            hint="algobot.engine.lifecycle is not deployed yet (parallel "
                 "workstream); retry once the engine service ships it.",
        ) from e
    try:
        lifecycle.set_mode(strategy_id, Mode(target_mode), actor=actor, force=force)
    except KeyError as e:
        # registered plugin but no DB row yet (engine seed hasn't run)
        raise UnknownStrategyError(str(e).strip("'\"")) from e
    log.info("set_mode %s -> %s (force=%s, actor=%s)",
             strategy_id, target_mode, force, actor)
    return {"strategy_id": strategy_id, "mode": target_mode,
            "force": force, "actor": actor, "ok": True}


def do_killswitch(on: bool, reason: str = "") -> dict:
    """Flip the global kill switch on today's risk_state row."""
    from algobot.execution.risk import RiskEngine  # lazy: keeps API boot light
    RiskEngine().set_kill_switch(bool(on), reason or "")
    log.warning("kill switch set to %s via API (%s)", on, reason or "no reason")
    return {"kill_switch": bool(on), "reason": reason or None, "ok": True}


def do_evaluate_gates(strategy_id: Optional[str] = None) -> dict:
    """Run the paper-to-live gate evaluation (one strategy or all)."""
    if strategy_id is not None and not readers.strategy_exists(strategy_id):
        raise UnknownStrategyError(f"unknown strategy '{strategy_id}'")
    try:
        from algobot.engine import gate
    except ImportError as e:
        raise EngineUnavailableError(
            f"engine gate module unavailable: {e}",
            hint="algobot.engine.gate is not deployed yet (parallel "
                 "workstream); read current results from GET /gates.",
        ) from e
    if strategy_id is not None:
        gate.evaluate(strategy_id)
    else:
        gate.evaluate_all()
    return {"evaluated": strategy_id or "all", "ok": True,
            "gates": readers.list_gates()}


def _gate_detail(strategy_id: str) -> dict:
    """detail_json of the strategy's gate row (for 409 responses)."""
    for g in readers.list_gates():
        if g["strategy_id"] == strategy_id:
            return g.get("detail_json") or {}
    return {}


# --------------------------------------------------------------------------- request models
class PromoteBody(BaseModel):
    """Promotion request. live requires a passing gate unless force=True."""
    target_mode: Literal["live", "paper"]
    force: bool = False


class DemoteBody(BaseModel):
    """Demotion request. Never gated — getting safer is always allowed."""
    target_mode: Literal["paper", "off"]


class KillSwitchBody(BaseModel):
    on: bool
    reason: str = ""


class EvaluateGatesBody(BaseModel):
    strategy_id: Optional[str] = None


# --------------------------------------------------------------------------- routes
def _http_from(e: Exception) -> HTTPException:
    if isinstance(e, UnknownStrategyError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, EngineUnavailableError):
        return HTTPException(status_code=503,
                             detail={"error": str(e), "hint": e.hint})
    raise e


@router.post("/strategies/{strategy_id}/promote")
def promote(strategy_id: str, body: PromoteBody) -> dict:
    """Promote a strategy (paper -> live, or off/backtest -> paper).

    409 when the paper-to-live gate blocks it (response carries the gate row's
    detail_json); 404 for unknown ids; 503 while the engine module is missing.
    """
    try:
        return do_set_mode(strategy_id, body.target_mode, force=body.force,
                           actor="api")
    except GateError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": str(e), "strategy_id": strategy_id,
                    "target_mode": body.target_mode,
                    "gate": _gate_detail(strategy_id),
                    "hint": "pass force=true to override the gate (not recommended)"},
        ) from e
    except (UnknownStrategyError, EngineUnavailableError) as e:
        raise _http_from(e) from e


@router.post("/strategies/{strategy_id}/demote")
def demote(strategy_id: str, body: DemoteBody) -> dict:
    """Demote a strategy to paper or off. Never gated (force=True always)."""
    try:
        return do_set_mode(strategy_id, body.target_mode, force=True, actor="api")
    except (UnknownStrategyError, EngineUnavailableError) as e:
        raise _http_from(e) from e


@router.post("/killswitch")
def killswitch(body: KillSwitchBody) -> dict:
    """Flip the global kill switch (blocks all new entries while on)."""
    return do_killswitch(body.on, body.reason)


@router.post("/gates/evaluate")
def evaluate_gates(body: EvaluateGatesBody = EvaluateGatesBody()) -> dict:
    """Re-run gate evaluation for one strategy (or all when omitted)."""
    try:
        return do_evaluate_gates(body.strategy_id)
    except (UnknownStrategyError, EngineUnavailableError) as e:
        raise _http_from(e) from e
