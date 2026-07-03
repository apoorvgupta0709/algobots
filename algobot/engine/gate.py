"""Automated paper-to-live gate (compendium §8.2).

Evaluated nightly per strategy from its paper track record + latest backtest:

- sample size: enough paper trades OR enough (discount-weighted) out-of-sample
  backtest months;
- edge: profit factor after the full Indian cost stack;
- risk: max drawdown of the paper equity curve;
- execution honesty: stop-fire fidelity — how far actual exit fills land from
  the modeled stop level.

The gate only ever flips ``gate_status.eligible``; promotion itself stays an
explicit human action through :func:`algobot.engine.lifecycle.set_mode`.
"""
from __future__ import annotations

import datetime as dt
import logging

from algobot.core import config, registry
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import (
    BacktestRunRow,
    EventLogRow,
    GateStatusRow,
    StrategyRow,
    TradeRow,
)

log = logging.getLogger(__name__)

#: Cap stored/compared profit factor so an all-win sample stays JSON/DB safe.
PF_CAP = 999.0
DEFAULT_CAPITAL = 100_000.0


# --------------------------------------------------------------------------- metrics
def _profit_factor(nets: list[float]) -> float | None:
    """Gross wins / gross losses, capped at PF_CAP. None with no trades."""
    if not nets:
        return None
    gross_win = sum(p for p in nets if p > 0)
    gross_loss = abs(sum(p for p in nets if p < 0))
    if gross_loss <= 0:
        return PF_CAP if gross_win > 0 else 0.0
    return round(min(gross_win / gross_loss, PF_CAP), 3)


def _max_drawdown_pct(nets: list[float], capital: float) -> float | None:
    """Max peak-to-trough drawdown (%) of capital + cumulative net P&L."""
    if not nets or capital <= 0:
        return None
    equity = capital
    peak = capital
    worst = 0.0
    for pnl in nets:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100.0)
    return round(worst, 3)


def _stop_fire_fidelity_pct(trades: list[TradeRow]) -> float | None:
    """Mean |exit - modeled_exit| / modeled_exit in %, over trades that carry a
    modeled exit price. None without samples."""
    samples = [abs(t.exit_price - t.modeled_exit_price) / abs(t.modeled_exit_price) * 100.0
               for t in trades
               if t.modeled_exit_price not in (None, 0)]
    if not samples:
        return None
    return round(sum(samples) / len(samples), 4)


def _oos_backtest_months(session, strategy_id: str, discount: float
                         ) -> tuple[float, dict]:
    """Discount-weighted months covered by the latest backtest run."""
    run = (session.query(BacktestRunRow)
           .filter_by(strategy_id=strategy_id)
           .order_by(BacktestRunRow.created_at.desc(), BacktestRunRow.id.desc())
           .first())
    if run is None:
        return 0.0, {"run_id": None}
    months = max((run.end - run.start).days, 0) / 30.0
    weight = 1.0 if run.data_source == "real" else float(discount)
    return round(months * weight, 3), {
        "run_id": run.id, "raw_months": round(months, 3),
        "data_source": run.data_source, "weight": weight}


# --------------------------------------------------------------------------- evaluate
def evaluate(strategy_id: str) -> GateStatusRow:
    """Evaluate one strategy's gate and upsert its :class:`GateStatusRow`.

    Eligibility changes are journalled to event_log.
    """
    init_db()
    cfg = config.gate_config()
    with session_scope() as s:
        trades = (s.query(TradeRow)
                  .filter_by(strategy_id=strategy_id, mode="paper")
                  .order_by(TradeRow.exit_time, TradeRow.id)
                  .all())
        strat_row = s.get(StrategyRow, strategy_id)
        capital = float(strat_row.capital_alloc) if strat_row is not None \
            else DEFAULT_CAPITAL

        nets = [float(t.net_pnl) for t in trades]
        n_trades = len(nets)
        pf = _profit_factor(nets)
        max_dd = _max_drawdown_pct(nets, capital)
        fidelity = _stop_fire_fidelity_pct(trades)
        oos_months, oos_detail = _oos_backtest_months(
            s, strategy_id, cfg["synthetic_backtest_discount"])

        trades_ok = n_trades >= int(cfg["min_paper_trades"])
        oos_ok = oos_months >= float(cfg["min_oos_backtest_months"])
        sample_ok = trades_ok or oos_ok
        pf_ok = pf is not None and pf >= float(cfg["min_profit_factor"])
        dd_ok = max_dd is not None and max_dd <= float(cfg["max_drawdown_pct"])
        fidelity_ok = fidelity is None or fidelity <= float(cfg["stop_fire_tolerance_pct"])
        eligible = sample_ok and pf_ok and dd_ok and fidelity_ok

        detail = {
            "paper_trades": {"value": n_trades,
                             "min": int(cfg["min_paper_trades"]),
                             "pass": trades_ok},
            "oos_backtest_months": {"value": oos_months,
                                    "min": float(cfg["min_oos_backtest_months"]),
                                    "pass": oos_ok, **oos_detail},
            "sample": {"pass": sample_ok,
                       "rule": "paper_trades OR oos_backtest_months"},
            "profit_factor": {"value": pf,
                              "min": float(cfg["min_profit_factor"]),
                              "pass": pf_ok},
            "max_drawdown_pct": {"value": max_dd,
                                 "max": float(cfg["max_drawdown_pct"]),
                                 "pass": dd_ok,
                                 "capital_base": capital},
            "stop_fire_fidelity_pct": {"value": fidelity,
                                       "max": float(cfg["stop_fire_tolerance_pct"]),
                                       "pass": fidelity_ok,
                                       "note": "no samples yet" if fidelity is None else None},
        }

        row = s.get(GateStatusRow, strategy_id)
        was_eligible = bool(row.eligible) if row is not None else False
        if row is None:
            row = GateStatusRow(strategy_id=strategy_id)
            s.add(row)
        row.paper_trades_count = n_trades
        row.oos_backtest_months = oos_months
        row.profit_factor = pf
        row.max_drawdown_pct = max_dd
        row.stop_fire_fidelity_pct = fidelity
        row.eligible = eligible
        row.detail_json = detail
        row.evaluated_at = dt.datetime.utcnow()

        if eligible != was_eligible:
            s.add(EventLogRow(
                source="gate", level="info",
                message=(f"{strategy_id}: gate eligibility "
                         f"{'GAINED' if eligible else 'LOST'} "
                         f"(pf={pf}, dd={max_dd}%, trades={n_trades}, "
                         f"oos={oos_months}m, fidelity={fidelity}%)"),
                detail_json=detail))
            log.info("gate %s: eligible %s -> %s", strategy_id,
                     was_eligible, eligible)
        s.flush()
        return row


def evaluate_all() -> dict[str, bool]:
    """Evaluate every registered strategy; one failure never blocks the rest."""
    results: dict[str, bool] = {}
    for sid in registry.all_strategies():
        try:
            results[sid] = bool(evaluate(sid).eligible)
        except Exception as exc:
            log.exception("gate evaluation failed for %s", sid)
            results[sid] = False
            try:
                with session_scope() as s:
                    s.add(EventLogRow(source="gate", level="error",
                                      message=f"gate evaluation failed for {sid}: {exc}"))
            except Exception:
                log.exception("failed to journal gate failure for %s", sid)
    return results
