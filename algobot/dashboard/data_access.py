"""All dashboard DB reads as plain, unit-testable functions.

Every function opens a short read-only session and returns a pandas DataFrame
(or a plain dict) — no Streamlit imports here. Pages wrap these with
``st.cache_data(ttl=10)`` (see ``algobot.dashboard.ui``).

Empty tables (a fresh DB) always yield empty DataFrames with the documented
columns, never an exception.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from algobot.core import clock, registry
from algobot.core.config import settings
from algobot.persistence.db import init_db, session_scope
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

# ---------------------------------------------------------------- column specs

POSITION_COLUMNS = [
    "id", "strategy_id", "mode", "symbol", "qty", "avg_price", "last_price",
    "unrealized_pnl", "stop_loss", "take_profit", "product_type", "opened_at",
]
TRADE_COLUMNS = [
    "id", "strategy_id", "mode", "symbol", "direction", "qty", "entry_time",
    "exit_time", "entry_price", "exit_price", "gross_pnl", "costs", "net_pnl",
    "exit_reason",
]
TODAYS_PNL_COLUMNS = ["strategy_id", "mode", "net_pnl", "gross_pnl", "trades"]
EQUITY_COLUMNS = ["ts", "strategy_id", "mode", "equity", "day_pnl"]
OVERVIEW_COLUMNS = [
    "strategy_id", "name", "category", "timeframe", "mode", "enabled",
    "capital_alloc", "eligible", "evaluated_at", "description",
]
GATE_COLUMNS = [
    "strategy_id", "mode", "paper_trades_count", "oos_backtest_months",
    "profit_factor", "max_drawdown_pct", "stop_fire_fidelity_pct", "eligible",
    "detail_json", "evaluated_at", "promoted_at", "promoted_by",
]
BACKTEST_COLUMNS = [
    "id", "strategy_id", "start", "end", "data_source", "cost_model_version",
    "profit_factor", "max_drawdown_pct", "sharpe", "trades", "net_pnl",
    "params_json", "metrics_json", "created_at",
]
EVENT_COLUMNS = ["id", "ts", "level", "source", "message", "detail_json"]


def _df(records: list[dict], columns: list[str]) -> pd.DataFrame:
    """DataFrame with a stable column set even when there are no rows."""
    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame.from_records(records, columns=columns)


def _today_bounds() -> tuple[dt.datetime, dt.datetime]:
    """Naive [00:00, 24:00) bounds of the current IST calendar day.

    DB timestamps are stored naive; the platform runs on IST wall time.
    """
    day = clock.now_ist().date()
    start = dt.datetime.combine(day, dt.time.min)
    return start, start + dt.timedelta(days=1)


# -------------------------------------------------------------------- readers

def open_positions(mode: str | None = None) -> pd.DataFrame:
    """Open positions, optionally filtered by mode ('paper'|'live')."""
    init_db()
    with session_scope() as s:
        q = s.query(PositionRow).filter(PositionRow.status == "open")
        if mode:
            q = q.filter(PositionRow.mode == mode)
        rows = q.order_by(PositionRow.strategy_id, PositionRow.opened_at).all()
    records = []
    for p in rows:
        unrealized = p.unrealized_pnl
        if unrealized is None and p.last_price is not None:
            unrealized = (p.last_price - p.avg_price) * p.qty
        records.append({
            "id": p.id, "strategy_id": p.strategy_id, "mode": p.mode,
            "symbol": p.symbol, "qty": p.qty, "avg_price": p.avg_price,
            "last_price": p.last_price, "unrealized_pnl": unrealized,
            "stop_loss": p.stop_loss, "take_profit": p.take_profit,
            "product_type": p.product_type, "opened_at": p.opened_at,
        })
    return _df(records, POSITION_COLUMNS)


def trades(mode: str | None = None, strategy_id: str | None = None,
           days: int | None = 30) -> pd.DataFrame:
    """Closed trades (journal), newest first. ``days=None`` returns everything."""
    init_db()
    with session_scope() as s:
        q = s.query(TradeRow)
        if mode:
            q = q.filter(TradeRow.mode == mode)
        if strategy_id:
            q = q.filter(TradeRow.strategy_id == strategy_id)
        if days is not None:
            cutoff = dt.datetime.combine(
                clock.now_ist().date() - dt.timedelta(days=days), dt.time.min)
            q = q.filter(TradeRow.exit_time >= cutoff)
        rows = q.order_by(TradeRow.exit_time.desc()).all()
    records = [{
        "id": t.id, "strategy_id": t.strategy_id, "mode": t.mode,
        "symbol": t.symbol, "direction": t.direction, "qty": t.qty,
        "entry_time": t.entry_time, "exit_time": t.exit_time,
        "entry_price": t.entry_price, "exit_price": t.exit_price,
        "gross_pnl": t.gross_pnl, "costs": t.costs, "net_pnl": t.net_pnl,
        "exit_reason": t.exit_reason,
    } for t in rows]
    return _df(records, TRADE_COLUMNS)


def todays_pnl_by_strategy() -> pd.DataFrame:
    """Net/gross P&L and trade count per (strategy, mode) for the IST day."""
    init_db()
    start, end = _today_bounds()
    with session_scope() as s:
        rows = (s.query(TradeRow)
                .filter(TradeRow.exit_time >= start, TradeRow.exit_time < end)
                .all())
    if not rows:
        return _df([], TODAYS_PNL_COLUMNS)
    df = pd.DataFrame.from_records([{
        "strategy_id": t.strategy_id, "mode": t.mode,
        "net_pnl": t.net_pnl, "gross_pnl": t.gross_pnl,
    } for t in rows])
    out = (df.groupby(["strategy_id", "mode"], as_index=False)
             .agg(net_pnl=("net_pnl", "sum"), gross_pnl=("gross_pnl", "sum"),
                  trades=("net_pnl", "size")))
    return out[TODAYS_PNL_COLUMNS]


def equity_curves(strategy_id: str | None = None, points: int = 500) -> pd.DataFrame:
    """Equity snapshots, downsampled to at most ``points`` per (strategy, mode)."""
    init_db()
    with session_scope() as s:
        q = s.query(EquitySnapshotRow)
        if strategy_id:
            q = q.filter(EquitySnapshotRow.strategy_id == strategy_id)
        rows = q.order_by(EquitySnapshotRow.ts).all()
    records = [{
        "ts": r.ts, "strategy_id": r.strategy_id, "mode": r.mode,
        "equity": r.equity, "day_pnl": r.day_pnl,
    } for r in rows]
    df = _df(records, EQUITY_COLUMNS)
    if df.empty or points <= 0:
        return df
    sampled = []
    for _, g in df.groupby(["strategy_id", "mode"], sort=False):
        if len(g) > points:
            idx = np.unique(np.linspace(0, len(g) - 1, points).round().astype(int))
            g = g.iloc[idx]
        sampled.append(g)
    return pd.concat(sampled).sort_values("ts").reset_index(drop=True)


def strategies_overview() -> pd.DataFrame:
    """StrategyRow state joined with registry metadata and gate eligibility.

    Includes registry strategies that have no DB row yet (mode 'off').
    """
    init_db()
    with session_scope() as s:
        db_rows = {r.strategy_id: r for r in s.query(StrategyRow).all()}
        gate_rows = {g.strategy_id: g for g in s.query(GateStatusRow).all()}
    try:
        reg = registry.all_strategies()
    except Exception:  # registry breakage must not blank the dashboard
        reg = {}
    records = []
    for sid in sorted(set(db_rows) | set(reg)):
        row = db_rows.get(sid)
        cls = reg.get(sid)
        meta = cls.meta if cls else None
        gate = gate_rows.get(sid)
        records.append({
            "strategy_id": sid,
            "name": meta.name if meta else sid,
            "category": (row.category if row else
                         (meta.category.value if meta else "")),
            "timeframe": meta.timeframe.value if meta else "",
            "mode": row.mode if row else "off",
            "enabled": bool(row.enabled) if row else False,
            "capital_alloc": (row.capital_alloc if row else
                              (meta.capital_required if meta else 0.0)),
            "eligible": bool(gate.eligible) if gate else False,
            "evaluated_at": gate.evaluated_at if gate else None,
            "description": meta.description if meta else "",
        })
    return _df(records, OVERVIEW_COLUMNS)


def gate_details() -> pd.DataFrame:
    """Full gate_status board, with the strategy's current mode joined in."""
    init_db()
    with session_scope() as s:
        gates = s.query(GateStatusRow).order_by(GateStatusRow.strategy_id).all()
        modes = {r.strategy_id: r.mode for r in s.query(StrategyRow).all()}
    records = [{
        "strategy_id": g.strategy_id,
        "mode": modes.get(g.strategy_id, "off"),
        "paper_trades_count": g.paper_trades_count,
        "oos_backtest_months": g.oos_backtest_months,
        "profit_factor": g.profit_factor,
        "max_drawdown_pct": g.max_drawdown_pct,
        "stop_fire_fidelity_pct": g.stop_fire_fidelity_pct,
        "eligible": bool(g.eligible),
        "detail_json": g.detail_json or {},
        "evaluated_at": g.evaluated_at,
        "promoted_at": g.promoted_at,
        "promoted_by": g.promoted_by,
    } for g in gates]
    return _df(records, GATE_COLUMNS)


def risk_today() -> dict[str, Any]:
    """Today's risk_state row plus the configured caps (absolute rupees)."""
    init_db()
    today = clock.now_ist().date()
    with session_scope() as s:
        row = s.get(RiskStateRow, today)
    cfg = settings()
    capital = float(cfg.get("capital", 0))
    risk_cfg = cfg.get("risk", {})
    out: dict[str, Any] = {
        "date": today,
        "realized_day_pnl": 0.0,
        "realized_week_pnl": 0.0,
        "open_position_count": 0,
        "trades_today": 0,
        "kill_switch": False,
        "kill_reason": None,
        "capital": capital,
        "daily_loss_cap": capital * float(risk_cfg.get("daily_loss_cap_pct", 0)) / 100.0,
        "weekly_loss_cap": capital * float(risk_cfg.get("weekly_loss_cap_pct", 0)) / 100.0,
        "max_concurrent_positions": int(risk_cfg.get("max_concurrent_positions", 0)),
        "max_trades_per_day": int(risk_cfg.get("max_trades_per_day", 0)),
    }
    if row is not None:
        out.update({
            "realized_day_pnl": row.realized_day_pnl,
            "realized_week_pnl": row.realized_week_pnl,
            "open_position_count": row.open_position_count,
            "trades_today": row.trades_today,
            "kill_switch": bool(row.kill_switch),
            "kill_reason": row.kill_reason,
        })
    return out


def _metric(m: dict, *keys: str) -> Any:
    for k in keys:
        if k in m and m[k] is not None:
            return m[k]
    return None


def backtest_runs(strategy_id: str | None = None) -> pd.DataFrame:
    """Backtest runs, newest first, with headline metrics lifted out of metrics_json."""
    init_db()
    with session_scope() as s:
        q = s.query(BacktestRunRow)
        if strategy_id:
            q = q.filter(BacktestRunRow.strategy_id == strategy_id)
        rows = q.order_by(BacktestRunRow.created_at.desc()).all()
    records = []
    for r in rows:
        m = r.metrics_json or {}
        records.append({
            "id": r.id, "strategy_id": r.strategy_id,
            "start": r.start, "end": r.end,
            "data_source": r.data_source,
            "cost_model_version": r.cost_model_version,
            "profit_factor": _metric(m, "profit_factor", "pf"),
            "max_drawdown_pct": _metric(m, "max_drawdown_pct", "max_dd_pct", "drawdown_pct"),
            "sharpe": _metric(m, "sharpe", "sharpe_ratio"),
            "trades": _metric(m, "trades", "n_trades", "num_trades", "trade_count"),
            "net_pnl": _metric(m, "net_pnl", "total_net_pnl", "pnl"),
            "params_json": r.params_json or {},
            "metrics_json": m,
            "created_at": r.created_at,
        })
    return _df(records, BACKTEST_COLUMNS)


def events(limit: int = 200) -> pd.DataFrame:
    """Most recent event_log rows, newest first."""
    init_db()
    with session_scope() as s:
        rows = (s.query(EventLogRow)
                .order_by(EventLogRow.ts.desc(), EventLogRow.id.desc())
                .limit(limit).all())
    records = [{
        "id": e.id, "ts": e.ts, "level": e.level, "source": e.source,
        "message": e.message, "detail_json": e.detail_json,
    } for e in rows]
    return _df(records, EVENT_COLUMNS)
