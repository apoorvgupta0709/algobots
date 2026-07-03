"""Performance metrics for backtest runs."""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import pandas as pd

from algobot.core.models import Trade

TRADING_DAYS = 252


def compute_metrics(trades: Sequence[Trade], equity: pd.Series) -> dict:
    """Standard performance stats from closed trades + the per-bar equity curve.

    Returns: net_pnl, gross_pnl, total_costs, n_trades, win_rate (0-1),
    profit_factor, expectancy_r (net expectancy in avg-loss R units), avg_win,
    avg_loss, max_drawdown_pct, sharpe (daily, annualised), cagr_pct,
    exposure (fraction of the tested span with an open trade).
    """
    nets = [t.net_pnl for t in trades]
    wins = [p for p in nets if p > 0]
    losses = [p for p in nets if p < 0]
    n = len(nets)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else \
        (math.inf if gross_win > 0 else 0.0)
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    expectancy_r = (float(np.mean(nets)) / abs(avg_loss)) if avg_loss else 0.0

    max_dd = sharpe = cagr = 0.0
    if len(equity) >= 2 and float(equity.iloc[0]) > 0:
        peak = equity.cummax()
        dd = (equity / peak - 1.0).min()
        max_dd = float(-dd * 100.0)
        daily = equity.resample("1D").last().dropna()
        rets = daily.pct_change().dropna()
        if len(rets) >= 2 and float(rets.std()) > 0:
            sharpe = float(rets.mean() / rets.std() * math.sqrt(TRADING_DAYS))
        span_days = max((equity.index[-1] - equity.index[0]).days, 1)
        start, end = float(equity.iloc[0]), float(equity.iloc[-1])
        if start > 0 and end > 0:
            cagr = ((end / start) ** (365.0 / span_days) - 1.0) * 100.0

    return {
        "net_pnl": round(float(sum(nets)), 2),
        "gross_pnl": round(float(sum(t.gross_pnl for t in trades)), 2),
        "total_costs": round(float(sum(t.costs for t in trades)), 2),
        "n_trades": n,
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else profit_factor,
        "expectancy_r": round(expectancy_r, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 3),
        "cagr_pct": round(cagr, 2),
        "exposure": round(_exposure(trades, equity), 4),
    }


def _exposure(trades: Sequence[Trade], equity: pd.Series) -> float:
    """Fraction of the tested span covered by at least one open trade."""
    if not trades or len(equity) < 2:
        return 0.0
    span = (equity.index[-1] - equity.index[0]).total_seconds()
    if span <= 0:
        return 0.0
    intervals = sorted((pd.Timestamp(t.entry_time), pd.Timestamp(t.exit_time))
                       for t in trades)
    covered = 0.0
    cur_s, cur_e = intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            covered += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
    covered += (cur_e - cur_s).total_seconds()
    return min(covered / span, 1.0)
