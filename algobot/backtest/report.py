"""Persistence and reporting for backtest runs."""
from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Sequence

import pandas as pd

from algobot.core.models import Trade
from algobot.costs.india import COST_MODEL_VERSION
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import BacktestRunRow, BacktestTradeRow

log = logging.getLogger(__name__)


def _json_safe(value):
    """Coerce numpy/inf values into JSON-storable primitives."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):          # numpy scalar
        return _json_safe(value.item())
    return str(value)


def persist_run(strategy_id: str, params: dict, start: dt.date, end: dt.date,
                data_source: str, metrics: dict,
                trades: Sequence[Trade]) -> int:
    """Store one BacktestRunRow + its BacktestTradeRows; returns the run id."""
    init_db()
    with session_scope() as session:
        run = BacktestRunRow(
            strategy_id=strategy_id, params_json=_json_safe(params or {}),
            start=start, end=end, data_source=data_source,
            cost_model_version=COST_MODEL_VERSION,
            metrics_json=_json_safe(metrics or {}))
        session.add(run)
        session.flush()
        for t in trades:
            session.add(BacktestTradeRow(
                run_id=run.id, symbol=t.symbol, direction=t.direction,
                qty=int(t.qty), entry_time=t.entry_time, exit_time=t.exit_time,
                entry_price=float(t.entry_price), exit_price=float(t.exit_price),
                gross_pnl=float(t.gross_pnl), costs=float(t.costs),
                net_pnl=float(t.net_pnl), exit_reason=t.exit_reason.value))
        run_id = run.id
    log.info("Persisted backtest run %d for %s (%d trades)", run_id,
             strategy_id, len(trades))
    return run_id


def equity_figure(equity: pd.Series):
    """Plotly figure of the mark-to-market equity curve with drawdown shading."""
    import plotly.graph_objects as go  # lazy: plotting is optional at runtime

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=equity.index, y=equity.values, mode="lines",
                             name="equity", line={"width": 1.5}))
    peak = equity.cummax()
    fig.add_trace(go.Scatter(x=equity.index, y=peak.values, mode="lines",
                             name="peak", line={"width": 1, "dash": "dot"}))
    fig.update_layout(title="Backtest equity curve",
                      xaxis_title="time", yaxis_title="equity (INR)",
                      template="plotly_white", legend={"orientation": "h"})
    return fig
