"""Background worker answering Hermes query jobs.

A :class:`QueryWorker` daemon thread polls the ``query_jobs`` table, claims
one queued job at a time with an atomic ``UPDATE ... WHERE status='queued'``
and writes the answer back. All answers reuse :mod:`algobot.api.readers` and
the control helpers in :mod:`algobot.api.routes_control`, so the queue and
the REST routes can never drift apart.

Result envelope: ``{"type": <resolved type>, "data": <answer>, "note"?: str}``
on success; ``{"error": "..."}`` with ``status='error'`` when a job blows up.
The loop itself survives anything, including DB hiccups.

Tests drive :meth:`QueryWorker.process_once` directly instead of starting
the thread.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Callable, Optional

from sqlalchemy import update

from algobot.api import readers
from algobot.api.routes_control import do_evaluate_gates, do_killswitch, do_set_mode
from algobot.persistence.db import session_scope
from algobot.persistence.schema import QueryJobRow

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- typed handlers
def _require(params: dict, *keys: str) -> None:
    missing = [k for k in keys if k not in params]
    if missing:
        raise ValueError(f"missing required params: {missing} (got {sorted(params)})")


def _h_status(params: dict) -> dict:
    return readers.get_status()


def _h_pnl(params: dict) -> dict:
    return readers.get_pnl(sparkline=bool(params.get("sparkline", False)))


def _h_positions(params: dict) -> list:
    return readers.list_positions(mode=params.get("mode"),
                                  strategy_id=params.get("strategy_id"),
                                  status=params.get("status", "open"),
                                  limit=params.get("limit"))


def _h_trades(params: dict) -> list:
    def _date(key: str) -> Optional[dt.date]:
        v = params.get(key)
        return dt.date.fromisoformat(v) if isinstance(v, str) else v
    return readers.list_trades(mode=params.get("mode"),
                               strategy_id=params.get("strategy_id"),
                               start=_date("from"), end=_date("to"),
                               limit=params.get("limit"))


def _h_strategies(params: dict) -> list:
    return readers.list_strategies()


def _h_strategy_detail(params: dict) -> dict:
    _require(params, "strategy_id")
    detail = readers.get_strategy_detail(params["strategy_id"])
    if detail is None:
        raise ValueError(f"unknown strategy '{params['strategy_id']}'")
    return detail


def _h_gate_status(params: dict) -> list:
    return readers.list_gates()


def _h_backtests(params: dict) -> list:
    return readers.list_backtests(strategy_id=params.get("strategy_id"),
                                  limit=params.get("limit"))


def _h_risk(params: dict) -> dict:
    return readers.get_risk()


def _h_events(params: dict) -> list:
    return readers.list_events(level=params.get("level"),
                               source=params.get("source"),
                               limit=params.get("limit"))


def _h_promote(params: dict) -> dict:
    _require(params, "strategy_id", "target_mode")
    return do_set_mode(params["strategy_id"], params["target_mode"],
                       force=bool(params.get("force", False)), actor="hermes")


def _h_demote(params: dict) -> dict:
    _require(params, "strategy_id", "target_mode")
    return do_set_mode(params["strategy_id"], params["target_mode"],
                       force=True, actor="hermes")


def _h_killswitch(params: dict) -> dict:
    _require(params, "on")
    return do_killswitch(bool(params["on"]), str(params.get("reason", "")))


def _h_evaluate_gates(params: dict) -> dict:
    return do_evaluate_gates(params.get("strategy_id"))


HANDLERS: dict[str, Callable[[dict], object]] = {
    "status": _h_status,
    "pnl": _h_pnl,
    "positions": _h_positions,
    "trades": _h_trades,
    "strategies": _h_strategies,
    "strategy_detail": _h_strategy_detail,
    "gate_status": _h_gate_status,
    "backtests": _h_backtests,
    "risk": _h_risk,
    "events": _h_events,
    "promote": _h_promote,
    "demote": _h_demote,
    "killswitch": _h_killswitch,
    "evaluate_gates": _h_evaluate_gates,
}

#: Ordered keyword router for freeform {question} payloads. First match wins;
#: keywords are matched as substrings of the lowercased question.
QUESTION_ROUTES: list[tuple[tuple[str, ...], str]] = [
    (("pnl", "profit", "p&l"), "pnl"),
    (("position",), "positions"),
    (("trade",), "trades"),
    (("gate", "promot"), "gate_status"),
    (("risk", "kill"), "risk"),
    (("strateg",), "strategies"),
    (("backtest",), "backtests"),
    (("status", "health", "running"), "status"),
]


def route_question(question: str) -> tuple[Optional[str], Optional[str]]:
    """Map a freeform question to a typed query. Returns (type, note) or
    (None, None) when no keyword matches."""
    q = question.lower()
    for keywords, qtype in QUESTION_ROUTES:
        for kw in keywords:
            if kw in q:
                return qtype, f"freeform question routed to '{qtype}' (matched '{kw}')"
    return None, None


def execute_payload(payload: dict) -> dict:
    """Answer one job payload; returns the result envelope. Raises on bad
    typed queries (caller converts to a status='error' result)."""
    note: Optional[str] = None
    params = dict(payload.get("params") or {})
    qtype = payload.get("type")

    question = payload.get("question")
    if not qtype and question:
        qtype, note = route_question(str(question))
        if qtype is None:
            return {
                "type": "unmapped",
                "data": {
                    "answer": "couldn't map question to a query type",
                    "question": question,
                    "available_types": sorted(HANDLERS),
                    "hint": "re-ask with a keyword (pnl/positions/trades/gates/"
                            "risk/strategies/backtests/status) or POST a typed "
                            "query {type, params}",
                },
            }

    handler = HANDLERS.get(str(qtype))
    if handler is None:
        raise ValueError(f"unknown query type '{qtype}'; "
                         f"available: {sorted(HANDLERS)}")
    envelope: dict = {"type": qtype, "data": handler(params)}
    if note:
        envelope["note"] = note
    return envelope


# --------------------------------------------------------------------------- worker
class QueryWorker:
    """Daemon thread that answers queued query jobs one at a time."""

    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = float(poll_interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the daemon polling thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="query-worker", daemon=True)
        self._thread.start()
        log.info("query worker started (poll every %.1fs)", self.poll_interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit and join the thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("query worker stopped")

    def _run(self) -> None:
        """Poll loop. Survives everything — a DB hiccup only costs one cycle."""
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.process_once()
            except Exception:
                log.exception("query worker loop error — continuing")
            if not worked:
                self._stop.wait(self.poll_interval)

    # ------------------------------------------------------------ processing
    def _claim(self) -> Optional[str]:
        """Atomically claim the oldest queued job; None when queue is empty.

        Uses UPDATE ... WHERE id=:id AND status='queued' so two workers can
        never grab the same job (rowcount tells us who won the race).
        """
        with session_scope() as s:
            cand = (s.query(QueryJobRow.id)
                    .filter(QueryJobRow.status == "queued")
                    .order_by(QueryJobRow.created_at.asc())
                    .first())
            if cand is None:
                return None
            job_id = cand[0]
            res = s.execute(
                update(QueryJobRow)
                .where(QueryJobRow.id == job_id,
                       QueryJobRow.status == "queued")
                .values(status="running"))
            if res.rowcount != 1:  # lost the race to another worker
                return None
            return job_id

    def process_once(self) -> bool:
        """Claim and answer one job. Returns True when a job was processed.

        A failing job is recorded as ``{"error": ...}`` with status='error';
        it never propagates out of this method's job-handling section.
        """
        job_id = self._claim()
        if job_id is None:
            return False

        with session_scope() as s:
            row = s.get(QueryJobRow, job_id)
            payload = dict(row.payload_json or {})

        try:
            result = execute_payload(payload)
            status = "done"
        except Exception as e:  # per-job catch-all: bad job != dead worker
            log.exception("query job %s failed", job_id)
            result = {"error": f"{type(e).__name__}: {e}"}
            status = "error"

        with session_scope() as s:
            row = s.get(QueryJobRow, job_id)
            row.status = status
            row.result_json = result
            row.finished_at = dt.datetime.utcnow()
        log.info("query job %s -> %s (type=%s)", job_id, status,
                 result.get("type", "?") if isinstance(result, dict) else "?")
        return True
