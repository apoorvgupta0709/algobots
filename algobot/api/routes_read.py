"""Read-only REST routes. Thin HTTP shims over :mod:`algobot.api.readers`
(the same functions back the query worker, so both surfaces always agree)."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from algobot.api import readers

router = APIRouter(tags=["read"])


@router.get("/status")
def get_status() -> dict:
    """Platform snapshot: strategy modes, positions, today's P&L, engine liveness."""
    return readers.get_status()


@router.get("/strategies")
def list_strategies() -> list[dict]:
    """All strategies (DB rows merged with plugin metadata)."""
    return readers.list_strategies()


@router.get("/strategies/{strategy_id}")
def get_strategy_detail(strategy_id: str) -> dict:
    """One strategy: mode, meta, gate, open positions, recent trades."""
    detail = readers.get_strategy_detail(strategy_id)
    if detail is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown strategy '{strategy_id}'")
    return detail


@router.get("/positions")
def list_positions(
    mode: Optional[str] = Query(None, description="paper|live"),
    strategy_id: Optional[str] = None,
    status: str = Query("open", description="open|closed|all"),
    limit: int = Query(readers.DEFAULT_LIMIT, ge=1, le=readers.MAX_LIMIT),
) -> list[dict]:
    """Positions (open by default), filterable by mode/strategy."""
    return readers.list_positions(mode=mode, strategy_id=strategy_id,
                                  status=status, limit=limit)


@router.get("/trades")
def list_trades(
    mode: Optional[str] = Query(None, description="paper|live"),
    strategy_id: Optional[str] = None,
    from_: Optional[dt.date] = Query(None, alias="from",
                                     description="exit date >= (YYYY-MM-DD)"),
    to: Optional[dt.date] = Query(None, description="exit date <= (YYYY-MM-DD)"),
    limit: int = Query(readers.DEFAULT_LIMIT, ge=1, le=readers.MAX_LIMIT),
) -> list[dict]:
    """Closed trades, newest first, filterable by mode/strategy/date range."""
    return readers.list_trades(mode=mode, strategy_id=strategy_id,
                               start=from_, end=to, limit=limit)


@router.get("/pnl")
def get_pnl(sparkline: bool = Query(False, description="include equity_snapshots tail")) -> dict:
    """Today/week/month net P&L per strategy+mode (IST boundaries)."""
    return readers.get_pnl(sparkline=sparkline)


@router.get("/gates")
def list_gates() -> list[dict]:
    """Paper-to-live gate status per strategy."""
    return readers.list_gates()


@router.get("/backtests")
def list_backtests(
    strategy_id: Optional[str] = None,
    limit: int = Query(readers.DEFAULT_LIMIT, ge=1, le=readers.MAX_LIMIT),
) -> list[dict]:
    """Backtest runs, newest first."""
    return readers.list_backtests(strategy_id=strategy_id, limit=limit)


@router.get("/risk")
def get_risk() -> dict:
    """Today's risk state, configured caps and the kill switch."""
    return readers.get_risk()


@router.get("/events")
def list_events(
    level: Optional[str] = Query(None, description="info|warning|error"),
    source: Optional[str] = None,
    limit: int = Query(readers.DEFAULT_LIMIT, ge=1, le=readers.MAX_LIMIT),
) -> list[dict]:
    """Audit-trail events, newest first."""
    return readers.list_events(level=level, source=source, limit=limit)
