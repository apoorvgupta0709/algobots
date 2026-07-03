"""AlgoBot API service (FastAPI). The only surface Hermes talks to.

Run: ``uvicorn algobot.api.main:app`` (docker-compose `api` service).

Three interaction styles:
1. direct REST reads   — GET /status /strategies /positions /trades /pnl ...
2. async job queue     — POST /queries -> 202 {id, poll}; a background
                         QueryWorker answers; GET /queries/{id} for the result
3. control actions     — POST /strategies/{id}/promote|demote, /killswitch,
                         /gates/evaluate

GET / returns a machine-readable catalog + usage guide so Hermes can
self-discover the whole surface.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from algobot.api import query_worker as qw
from algobot.api.routes_control import router as control_router
from algobot.api.routes_queries import router as queries_router
from algobot.api.routes_read import router as read_router
from algobot.core.clock import is_market_open, now_ist
from algobot.persistence.db import init_db, session_scope

log = logging.getLogger(__name__)

API_VERSION = "1.0"


def create_app(start_worker: bool = True) -> FastAPI:
    """Build the app. ``start_worker=False`` (or env
    ALGOBOT_API_DISABLE_WORKER=1) skips the QueryWorker thread — used by
    tests, which drive ``QueryWorker.process_once()`` directly."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        worker = None
        if start_worker and os.getenv("ALGOBOT_API_DISABLE_WORKER") != "1":
            worker = qw.QueryWorker()
            worker.start()
            app.state.query_worker = worker
        yield
        if worker is not None:
            worker.stop()

    app = FastAPI(title="AlgoBot API", version=API_VERSION, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(read_router)
    app.include_router(control_router)
    app.include_router(queries_router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        """Liveness + DB connectivity probe."""
        try:
            with session_scope() as s:
                s.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            log.exception("health check: DB unreachable")
            db_ok = False
        return {
            "status": "ok" if db_ok else "degraded",
            "market_open": is_market_open(),
            "time_ist": now_ist().isoformat(),
            "db_ok": db_ok,
        }

    @app.get("/", tags=["meta"])
    def root() -> dict:
        """Service info + endpoint catalog + usage guide (Hermes self-discovery)."""
        return {
            "service": "algobot-api",
            "version": API_VERSION,
            "description": "NSE algo-trading platform API: REST reads, an "
                           "async query queue, and control actions.",
            "time_ist": now_ist().isoformat(),
            "endpoints": {
                "GET /health": "liveness + db connectivity",
                "GET /status": "platform snapshot: modes, positions, today's P&L, engine liveness",
                "GET /strategies": "all strategies (DB state + plugin metadata)",
                "GET /strategies/{id}": "one strategy: mode, meta, gate, positions, recent trades",
                "GET /positions": "positions; filters: mode, strategy_id, status, limit",
                "GET /trades": "closed trades newest first; filters: mode, strategy_id, from, to, limit",
                "GET /pnl": "today/week/month net P&L per strategy+mode; ?sparkline=true adds equity tail",
                "GET /gates": "paper-to-live gate status per strategy",
                "GET /backtests": "backtest runs; filters: strategy_id, limit",
                "GET /risk": "today's risk state + configured caps + kill switch",
                "GET /events": "audit trail; filters: level, source, limit",
                "POST /strategies/{id}/promote": "{target_mode: live|paper, force?: bool} — 409 if gated, 503 if engine module missing",
                "POST /strategies/{id}/demote": "{target_mode: paper|off} — never gated",
                "POST /killswitch": "{on: bool, reason: str} — halt all new entries",
                "POST /gates/evaluate": "{strategy_id?: str} — re-run gate evaluation",
                "POST /queries": "enqueue a job: {type, params} or {question}; returns 202 {id, poll}",
                "GET /queries/{id}": "job status + result envelope {type, data, note?}",
                "GET /queries": "recent jobs; filters: status, limit",
            },
            "usage": {
                "job_queue": {
                    "flow": "POST /queries -> 202 {id, status: queued, poll} -> "
                            "poll GET /queries/{id} until status is done|error -> "
                            "read result {type, data, note?} (or {error})",
                    "typed_query_example": {"type": "trades",
                                            "params": {"mode": "live", "limit": 20}},
                    "freeform_example": {"question": "what's my pnl today?"},
                    "worker_poll_interval_s": 1.0,
                },
                "query_types": sorted(qw.HANDLERS),
                "freeform_routing": {
                    "how": "lowercased question matched against ordered keyword "
                           "groups; first hit picks the query type; no hit "
                           "returns available_types + a hint",
                    "routes": [{"keywords": list(kws), "type": t}
                               for kws, t in qw.QUESTION_ROUTES],
                },
                "control": {
                    "promote": "POST /strategies/{id}/promote — gate-checked; "
                               "409 carries the gate detail; force=true overrides",
                    "demote": "POST /strategies/{id}/demote — always allowed",
                    "killswitch": "POST /killswitch {on, reason} — also available "
                                  "as query type 'killswitch'",
                },
                "limits": "list endpoints accept ?limit= (default 100, max 500)",
                "timezone": "all trading-day boundaries are IST (Asia/Kolkata)",
            },
        }

    return app


#: module-level app for `uvicorn algobot.api.main:app`
app = create_app()
