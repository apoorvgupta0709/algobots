"""Shared read logic for the API service.

Single source of truth for BOTH the REST routes (routes_read.py) and the
query worker (query_worker.py): every reader returns plain JSON-safe data
(pydantic models dumped with ``mode="json"``), so results can go straight
into an HTTP response or a ``QueryJobRow.result_json`` column.

Conventions
-----------
- limits are clamped to ``MAX_LIMIT`` (500), default ``DEFAULT_LIMIT`` (100)
- today/week/month boundaries are computed in IST via
  :func:`algobot.core.clock.now_ist` / :func:`algobot.core.clock.week_start`
- engine liveness = latest ``event_log`` row with ``source == "engine"``
  younger than :data:`ENGINE_ALIVE_WINDOW_S` seconds
- registry meta lookups are wrapped in a catch-all so a broken strategy
  plugin can never take the API down
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func

from algobot.core.clock import is_market_open, now_ist, week_start
from algobot.core.config import settings
from algobot.persistence.db import session_scope
from algobot.persistence.schema import (
    BacktestRunRow,
    EquitySnapshotRow,
    EventLogRow,
    GateStatusRow,
    PositionRow,
    RiskStateRow,
    StrategyRow,
    TradeRow,
)

log = logging.getLogger(__name__)

#: an engine heartbeat (event_log row with source=="engine") older than this
#: many seconds means the engine is considered down.
ENGINE_ALIVE_WINDOW_S = 300

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def clamp_limit(limit: Optional[int]) -> int:
    """Clamp a row limit into [1, MAX_LIMIT]; None -> DEFAULT_LIMIT."""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


# --------------------------------------------------------------------------- output models
class StrategyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    strategy_id: str
    category: str
    mode: str
    params_json: dict = {}
    capital_alloc: float
    enabled: bool
    updated_at: Optional[dt.datetime] = None


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: str
    mode: str
    symbol: str
    qty: int
    avg_price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    underlying: Optional[str] = None
    product_type: str
    opened_at: dt.datetime
    status: str
    last_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: str
    mode: str
    symbol: str
    direction: str
    qty: int
    entry_time: dt.datetime
    exit_time: dt.datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str


class GateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    strategy_id: str
    paper_trades_count: int
    oos_backtest_months: float
    profit_factor: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    stop_fire_fidelity_pct: Optional[float] = None
    eligible: bool
    detail_json: dict = {}
    evaluated_at: Optional[dt.datetime] = None
    promoted_at: Optional[dt.datetime] = None
    promoted_by: Optional[str] = None


class BacktestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    strategy_id: str
    params_json: dict = {}
    start: dt.date
    end: dt.date
    data_source: str
    cost_model_version: str
    metrics_json: dict = {}
    created_at: Optional[dt.datetime] = None


class RiskStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: dt.date
    realized_day_pnl: float
    realized_week_pnl: float
    open_position_count: int
    trades_today: int
    kill_switch: bool
    kill_reason: Optional[str] = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: dt.datetime
    level: str
    source: str
    message: str
    detail_json: Optional[dict] = None


class EquityPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: dt.datetime
    strategy_id: str
    mode: str
    equity: float
    day_pnl: float


def _dump(model_cls: type[BaseModel], rows: list) -> list[dict]:
    return [model_cls.model_validate(r).model_dump(mode="json") for r in rows]


# --------------------------------------------------------------------------- boundaries
def _day_start() -> dt.datetime:
    """Naive midnight of today's IST date (DB datetimes are stored naive)."""
    return dt.datetime.combine(now_ist().date(), dt.time.min)


def _week_start_dt() -> dt.datetime:
    return dt.datetime.combine(week_start(), dt.time.min)


def _month_start_dt() -> dt.datetime:
    return dt.datetime.combine(now_ist().date().replace(day=1), dt.time.min)


# --------------------------------------------------------------------------- registry (guarded)
def registry_meta() -> dict[str, dict]:
    """strategy_id -> plugin metadata dict. NEVER raises: a broken plugin or a
    broken registry import degrades to missing metadata, not a 500."""
    out: dict[str, dict] = {}
    try:
        from algobot.core.registry import all_strategies
        classes = all_strategies()
    except Exception:
        log.exception("strategy registry unavailable — serving DB rows without meta")
        return out
    for sid, cls in classes.items():
        try:
            m = cls.meta
            out[sid] = {
                "name": m.name,
                "category": m.category.value,
                "timeframe": m.timeframe.value,
                "scan_schedule": m.scan_schedule,
                "instruments": list(m.instruments),
                "capital_required": m.capital_required,
                "max_positions": m.max_positions,
                "max_trades_per_day": m.max_trades_per_day,
                "intraday_squareoff": m.intraday_squareoff,
                "is_multi_leg": m.is_multi_leg,
                "description": m.description,
            }
        except Exception:
            log.exception("broken meta on strategy plugin %s — skipping", sid)
    return out


def strategy_exists(strategy_id: str) -> bool:
    """True when the id is known to the DB OR the plugin registry."""
    with session_scope() as s:
        if s.get(StrategyRow, strategy_id) is not None:
            return True
    return strategy_id in registry_meta()


# --------------------------------------------------------------------------- readers
def _engine_alive(s) -> bool:
    row = (s.query(EventLogRow.ts)
           .filter(EventLogRow.source == "engine")
           .order_by(EventLogRow.ts.desc())
           .first())
    if row is None or row[0] is None:
        return False
    return (dt.datetime.utcnow() - row[0]).total_seconds() < ENGINE_ALIVE_WINDOW_S


def get_status() -> dict:
    """Platform snapshot: strategy mode counts, open positions, today's trades
    and per-mode P&L, kill switch, engine liveness, market clock."""
    day_start = _day_start()
    with session_scope() as s:
        mode_counts = {m: int(c) for m, c in
                       s.query(StrategyRow.mode, func.count())
                       .group_by(StrategyRow.mode).all()}
        open_positions = int(
            s.query(func.count(PositionRow.id))
            .filter(PositionRow.status == "open").scalar() or 0)
        today_rows = (s.query(TradeRow.mode, func.count(TradeRow.id),
                              func.coalesce(func.sum(TradeRow.net_pnl), 0.0))
                      .filter(TradeRow.exit_time >= day_start)
                      .group_by(TradeRow.mode).all())
        risk = s.get(RiskStateRow, now_ist().date())
        engine_alive = _engine_alive(s)

    by_mode = {m: {"count": int(c), "net_pnl": round(float(p), 2)}
               for m, c, p in today_rows}
    return {
        "time_ist": now_ist().isoformat(),
        "market_open": is_market_open(),
        "engine_alive": engine_alive,
        "strategies": {"total": sum(mode_counts.values()), "by_mode": mode_counts},
        "open_positions": open_positions,
        "trades_today": {"count": sum(v["count"] for v in by_mode.values()),
                         "by_mode": by_mode},
        "pnl_today": {"total": round(sum(v["net_pnl"] for v in by_mode.values()), 2),
                      "by_mode": {m: v["net_pnl"] for m, v in by_mode.items()}},
        "kill_switch": bool(risk.kill_switch) if risk is not None else False,
        "kill_reason": risk.kill_reason if risk is not None else None,
    }


def list_strategies() -> list[dict]:
    """All strategies: DB rows merged with plugin metadata; registry-only
    strategies (not yet provisioned in the DB) are appended with in_db=False."""
    metas = registry_meta()
    with session_scope() as s:
        rows = s.query(StrategyRow).order_by(StrategyRow.strategy_id).all()
    items: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        d = StrategyOut.model_validate(row).model_dump(mode="json")
        d["in_db"] = True
        d["meta"] = metas.get(row.strategy_id)
        items.append(d)
        seen.add(row.strategy_id)
    for sid in sorted(set(metas) - seen):
        items.append({"strategy_id": sid, "mode": None, "enabled": None,
                      "in_db": False, "meta": metas[sid]})
    return items


def get_strategy_detail(strategy_id: str) -> Optional[dict]:
    """One strategy: DB row, plugin meta, gate status, open positions, recent
    trades and today's P&L. Returns None when the id is unknown everywhere."""
    meta = registry_meta().get(strategy_id)
    day_start = _day_start()
    with session_scope() as s:
        row = s.get(StrategyRow, strategy_id)
        if row is None and meta is None:
            return None
        gate = s.get(GateStatusRow, strategy_id)
        positions = (s.query(PositionRow)
                     .filter(PositionRow.strategy_id == strategy_id,
                             PositionRow.status == "open")
                     .order_by(PositionRow.opened_at.desc()).all())
        trades = (s.query(TradeRow)
                  .filter(TradeRow.strategy_id == strategy_id)
                  .order_by(TradeRow.exit_time.desc()).limit(20).all())
        pnl_today = float(
            s.query(func.coalesce(func.sum(TradeRow.net_pnl), 0.0))
            .filter(TradeRow.strategy_id == strategy_id,
                    TradeRow.exit_time >= day_start).scalar() or 0.0)
        strategy = (StrategyOut.model_validate(row).model_dump(mode="json")
                    if row is not None else None)
        return {
            "strategy_id": strategy_id,
            "in_db": row is not None,
            "strategy": strategy,
            "meta": meta,
            "gate": (GateOut.model_validate(gate).model_dump(mode="json")
                     if gate is not None else None),
            "open_positions": _dump(PositionOut, positions),
            "recent_trades": _dump(TradeOut, trades),
            "pnl_today": round(pnl_today, 2),
        }


def list_positions(mode: Optional[str] = None, strategy_id: Optional[str] = None,
                   status: str = "open", limit: Optional[int] = None) -> list[dict]:
    """Positions, open by default; pass status='closed' or 'all' to widen."""
    limit = clamp_limit(limit)
    with session_scope() as s:
        q = s.query(PositionRow)
        if status and status != "all":
            q = q.filter(PositionRow.status == status)
        if mode:
            q = q.filter(PositionRow.mode == mode)
        if strategy_id:
            q = q.filter(PositionRow.strategy_id == strategy_id)
        rows = q.order_by(PositionRow.opened_at.desc()).limit(limit).all()
    return _dump(PositionOut, rows)


def list_trades(mode: Optional[str] = None, strategy_id: Optional[str] = None,
                start: Optional[dt.date] = None, end: Optional[dt.date] = None,
                limit: Optional[int] = None) -> list[dict]:
    """Closed trades, newest first. start/end filter on exit_time (inclusive)."""
    limit = clamp_limit(limit)
    with session_scope() as s:
        q = s.query(TradeRow)
        if mode:
            q = q.filter(TradeRow.mode == mode)
        if strategy_id:
            q = q.filter(TradeRow.strategy_id == strategy_id)
        if start:
            q = q.filter(TradeRow.exit_time >= dt.datetime.combine(start, dt.time.min))
        if end:
            q = q.filter(TradeRow.exit_time <= dt.datetime.combine(end, dt.time.max))
        rows = q.order_by(TradeRow.exit_time.desc()).limit(limit).all()
    return _dump(TradeOut, rows)


def _pnl_window(s, since: dt.datetime) -> dict:
    rows = (s.query(TradeRow.strategy_id, TradeRow.mode,
                    func.count(TradeRow.id),
                    func.coalesce(func.sum(TradeRow.net_pnl), 0.0))
            .filter(TradeRow.exit_time >= since)
            .group_by(TradeRow.strategy_id, TradeRow.mode).all())
    breakdown = [{"strategy_id": sid, "mode": m, "trades": int(c),
                  "net_pnl": round(float(p), 2)} for sid, m, c, p in rows]
    by_mode: dict[str, float] = {}
    for b in breakdown:
        by_mode[b["mode"]] = round(by_mode.get(b["mode"], 0.0) + b["net_pnl"], 2)
    return {"since": since.isoformat(),
            "total": round(sum(b["net_pnl"] for b in breakdown), 2),
            "by_mode": by_mode,
            "by_strategy": sorted(breakdown, key=lambda b: (b["strategy_id"], b["mode"]))}


def get_pnl(sparkline: bool = False, sparkline_limit: int = 50) -> dict:
    """Net P&L per strategy+mode over today / this week / this month (IST
    boundaries). ``sparkline=True`` adds the tail of equity_snapshots."""
    with session_scope() as s:
        out: dict[str, Any] = {
            "as_of": now_ist().isoformat(),
            "today": _pnl_window(s, _day_start()),
            "week": _pnl_window(s, _week_start_dt()),
            "month": _pnl_window(s, _month_start_dt()),
        }
        if sparkline:
            tail = (s.query(EquitySnapshotRow)
                    .order_by(EquitySnapshotRow.ts.desc())
                    .limit(clamp_limit(sparkline_limit)).all())
            out["sparkline"] = _dump(EquityPointOut, list(reversed(tail)))
    return out


def list_gates() -> list[dict]:
    """Paper-to-live gate status for every evaluated strategy."""
    with session_scope() as s:
        rows = s.query(GateStatusRow).order_by(GateStatusRow.strategy_id).all()
    return _dump(GateOut, rows)


def list_backtests(strategy_id: Optional[str] = None,
                   limit: Optional[int] = None) -> list[dict]:
    """Backtest runs, newest first."""
    limit = clamp_limit(limit)
    with session_scope() as s:
        q = s.query(BacktestRunRow)
        if strategy_id:
            q = q.filter(BacktestRunRow.strategy_id == strategy_id)
        rows = q.order_by(BacktestRunRow.id.desc()).limit(limit).all()
    return _dump(BacktestOut, rows)


def get_risk() -> dict:
    """Today's risk state + the configured caps + kill switch."""
    cfg = settings()
    risk_cfg = dict(cfg["risk"])
    capital = float(cfg["capital"])
    with session_scope() as s:
        row = s.get(RiskStateRow, now_ist().date())
        state = (RiskStateOut.model_validate(row).model_dump(mode="json")
                 if row is not None else None)
    return {
        "date": now_ist().date().isoformat(),
        "state": state,
        "kill_switch": bool(state["kill_switch"]) if state else False,
        "kill_reason": state["kill_reason"] if state else None,
        "caps": {
            "capital": capital,
            "risk_per_trade_pct": risk_cfg["risk_per_trade_pct"],
            "daily_loss_cap_pct": risk_cfg["daily_loss_cap_pct"],
            "daily_loss_cap_rupees": round(capital * float(risk_cfg["daily_loss_cap_pct"]) / 100, 2),
            "weekly_loss_cap_pct": risk_cfg["weekly_loss_cap_pct"],
            "weekly_loss_cap_rupees": round(capital * float(risk_cfg["weekly_loss_cap_pct"]) / 100, 2),
            "max_concurrent_positions": risk_cfg["max_concurrent_positions"],
            "max_trades_per_day": risk_cfg["max_trades_per_day"],
        },
    }


def list_events(level: Optional[str] = None, source: Optional[str] = None,
                limit: Optional[int] = None) -> list[dict]:
    """Audit trail rows, newest first."""
    limit = clamp_limit(limit)
    with session_scope() as s:
        q = s.query(EventLogRow)
        if level:
            q = q.filter(EventLogRow.level == level)
        if source:
            q = q.filter(EventLogRow.source == source)
        rows = q.order_by(EventLogRow.id.desc()).limit(limit).all()
    return _dump(EventOut, rows)
