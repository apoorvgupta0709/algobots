#!/usr/bin/env python3
"""Backtest FTS_SWING_V1: Fundamental + Technical + Sentiment swing strategy.

This is research/backtest infrastructure only. It does not place, modify, or
cancel FYERS orders. Historical technical signals are computed from stored
candles. Until structured historical fundamental/sentiment evidence exists, the
backtest uses neutral F/S placeholders and records that limitation explicitly.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Sequence

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import compute_technical_factors as tech

DEFAULT_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")
DEFAULT_WATCHLIST = PROJECT_ROOT / "watchlists" / "active.csv"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"
STRATEGY_NAME = "FTS_SWING_V1"
STRATEGY_VERSION = "1.0"
TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class StrategyConfig:
    initial_capital: Decimal = Decimal("5000")
    max_risk_per_trade: Decimal = Decimal("50")
    max_position_value: Decimal = Decimal("2500")
    max_open_positions: int = 1
    max_holding_days: int = 3
    min_score: Decimal = Decimal("65")
    min_rr: Decimal = Decimal("1.75")
    target_r_multiple: Decimal = Decimal("2")
    stop_atr_multiple: Decimal = Decimal("1.25")
    cost_bps_per_side: Decimal = Decimal("5")
    slippage_bps_per_side: Decimal = Decimal("5")
    neutral_fundamental_score: Decimal = Decimal("15")
    neutral_sentiment_score: Decimal = Decimal("10")
    require_sma200: bool = True

    def to_json(self) -> dict[str, object]:
        return {
            "initial_capital": str(self.initial_capital),
            "max_risk_per_trade": str(self.max_risk_per_trade),
            "max_position_value": str(self.max_position_value),
            "max_open_positions": self.max_open_positions,
            "max_holding_days": self.max_holding_days,
            "min_score": str(self.min_score),
            "min_rr": str(self.min_rr),
            "target_r_multiple": str(self.target_r_multiple),
            "stop_atr_multiple": str(self.stop_atr_multiple),
            "cost_bps_per_side": str(self.cost_bps_per_side),
            "slippage_bps_per_side": str(self.slippage_bps_per_side),
            "neutral_fundamental_score": str(self.neutral_fundamental_score),
            "neutral_sentiment_score": str(self.neutral_sentiment_score),
            "require_sma200": self.require_sma200,
            "live_orders_enabled": False,
        }


@dataclass(frozen=True)
class EvidenceSnapshot:
    fundamental_label: str = "insufficient_data"
    sentiment_label: str = "insufficient_data"
    fundamental_score: Decimal | None = None
    sentiment_score: Decimal | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Candle:
    symbol: str
    resolution: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None


@dataclass(frozen=True)
class Signal:
    symbol: str
    ts: datetime
    label: str
    score: Decimal
    technical_score: Decimal
    fundamental_score: Decimal
    sentiment_score: Decimal
    risk_score: Decimal
    entry_trigger: Decimal
    stop_loss: Decimal | None
    target: Decimal | None
    reasons: list[str]
    risks: list[str]
    factors: dict[str, str]


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    side: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    reason_entry: str
    reason_exit: str
    score: Decimal
    raw: dict[str, object]


@dataclass(frozen=True)
class BacktestResult:
    strategy_name: str
    version: str
    universe: str
    resolution: str
    start_ts: datetime
    end_ts: datetime
    trades: list[BacktestTrade]
    metrics: dict[str, str | int]
    warnings: list[str]
    backtest_run_id: int | None = None


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def q6(value: Decimal) -> Decimal:
    return value.quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def dec(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def factor_decimal(factors: dict[str, str], key: str) -> Decimal | None:
    value = factors.get(key)
    return Decimal(str(value)) if value is not None else None


def as_tech_candles(candles: Sequence[Candle]) -> list[tech.Candle]:
    return [
        tech.Candle(
            symbol=c.symbol,
            resolution=c.resolution,
            ts=c.ts,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=c.volume,
        )
        for c in candles
    ]


def evidence_scores(config: StrategyConfig, evidence: EvidenceSnapshot | None) -> tuple[Decimal, Decimal, list[str], list[str]]:
    evidence = evidence or EvidenceSnapshot()
    reasons: list[str] = []
    risks: list[str] = []

    if evidence.fundamental_score is not None:
        fundamental_score = evidence.fundamental_score
        reasons.append(f"Fundamental evidence score supplied: {fundamental_score}")
    else:
        fundamental_score = config.neutral_fundamental_score
        reasons.append("Fundamental evidence: historical structured fundamentals unavailable; neutral placeholder used")

    if evidence.sentiment_score is not None:
        sentiment_score = evidence.sentiment_score
        reasons.append(f"Sentiment evidence score supplied: {sentiment_score}")
    else:
        sentiment_score = config.neutral_sentiment_score
        reasons.append("Sentiment evidence: historical structured sentiment/catalyst data unavailable; neutral placeholder used")

    if evidence.fundamental_label in {"weak", "negative", "event_risk"}:
        risks.append(f"Weak fundamental evidence label: {evidence.fundamental_label}")
    if evidence.sentiment_label in {"negative", "event_risk"}:
        risks.append(f"Negative sentiment/catalyst evidence label: {evidence.sentiment_label}")
    return fundamental_score, sentiment_score, reasons, risks


def evaluate_signal(
    history: Sequence[Candle],
    config: StrategyConfig,
    *,
    evidence: EvidenceSnapshot | None = None,
) -> Signal:
    if len(history) < 200:
        raise ValueError("FTS_SWING_V1 requires at least 200 candles for SMA200-aware scoring")

    latest = history[-1]
    snapshot = tech.compute_latest_snapshot(as_tech_candles(history))
    factors = snapshot.factors
    reasons: list[str] = []
    risks: list[str] = []
    technical = Decimal("0")
    risk_score = Decimal("5")

    close = latest.close
    sma20 = factor_decimal(factors, "sma_20")
    sma50 = factor_decimal(factors, "sma_50")
    sma200 = factor_decimal(factors, "sma_200")
    rsi14 = factor_decimal(factors, "rsi_14")
    atr14 = factor_decimal(factors, "atr_14")
    atr_pct = factor_decimal(factors, "atr_pct_14")
    relvol = factor_decimal(factors, "relative_volume_20")
    macd_hist = factor_decimal(factors, "macd_histogram")
    roc20 = factor_decimal(factors, "roc_20")

    if factors.get("trend") == "bullish" and sma20 is not None and sma50 is not None and close > sma20 > sma50:
        technical += Decimal("12")
        reasons.append("Technical: bullish trend with close above SMA20>SMA50")
    else:
        risks.append("Technical trend filter failed: close is not above SMA20>SMA50")

    if sma200 is not None and close > sma200:
        technical += Decimal("6")
        reasons.append("Technical: close above SMA200")
    elif config.require_sma200:
        risks.append("Technical long-term trend filter failed: close not above SMA200")

    if rsi14 is not None:
        if Decimal("50") <= rsi14 <= Decimal("68"):
            technical += Decimal("8")
            reasons.append(f"Technical: RSI constructive at {rsi14}")
        elif Decimal("45") <= rsi14 <= Decimal("72"):
            technical += Decimal("4")
            reasons.append(f"Technical: RSI acceptable but not ideal at {rsi14}")
        elif rsi14 > Decimal("75"):
            risks.append(f"Technical risk: RSI extended at {rsi14}")
        else:
            risks.append(f"Technical risk: RSI weak at {rsi14}")

    if macd_hist is not None and macd_hist > 0:
        technical += Decimal("6")
        reasons.append("Technical: MACD histogram positive")
    else:
        risks.append("Technical momentum filter failed: MACD histogram not positive")

    if roc20 is not None and roc20 > 0:
        technical += Decimal("4")
        reasons.append("Technical: ROC20 positive")

    if factors.get("breakout_20") == "yes" or factors.get("breakout_55") == "yes":
        technical += Decimal("4")
        reasons.append("Technical: Donchian breakout/reclaim present")

    if atr_pct is not None and Decimal("0.010") <= atr_pct <= Decimal("0.050"):
        risk_score += Decimal("5")
        reasons.append(f"Risk: ATR% tradable at {atr_pct}")
    elif atr_pct is not None and atr_pct > Decimal("0.060"):
        risks.append(f"Risk: volatility too high at ATR% {atr_pct}")
    elif atr_pct is not None:
        risks.append(f"Risk: volatility too low for swing target at ATR% {atr_pct}")

    if relvol is not None and relvol >= Decimal("0.60"):
        risk_score += Decimal("5")
        reasons.append(f"Risk: relative volume acceptable at {relvol}")
    elif relvol is not None:
        risks.append(f"Risk: low participation at relative volume {relvol}")

    fundamental, sentiment, evidence_reasons, evidence_risks = evidence_scores(config, evidence)
    reasons.extend(evidence_reasons)
    risks.extend(evidence_risks)

    stop_candidates = [value for value in [latest.low, close - ((atr14 or Decimal("0")) * config.stop_atr_multiple)] if value is not None]
    stop_loss = min(stop_candidates) if stop_candidates else None
    target = None
    if stop_loss is not None and stop_loss < close:
        target = close + ((close - stop_loss) * config.target_r_multiple)
    else:
        risks.append("Risk: valid stop could not be placed below entry trigger")

    technical = min(technical, Decimal("40"))
    risk_score = min(risk_score, Decimal("15"))
    score = q2(technical + fundamental + sentiment + risk_score)

    hard_review = any("Weak fundamental" in item or "Negative sentiment" in item for item in risks)
    if hard_review:
        label = "needs_review"
    elif score >= Decimal("80"):
        label = "high_conviction_paper"
    elif score >= config.min_score:
        label = "paper_setup"
    elif score >= Decimal("55"):
        label = "watch"
    else:
        label = "reject"

    return Signal(
        symbol=latest.symbol,
        ts=latest.ts,
        label=label,
        score=score,
        technical_score=q2(technical),
        fundamental_score=q2(fundamental),
        sentiment_score=q2(sentiment),
        risk_score=q2(risk_score),
        entry_trigger=close,
        stop_loss=q2(stop_loss) if stop_loss is not None else None,
        target=q2(target) if target is not None else None,
        reasons=reasons,
        risks=risks,
        factors=factors,
    )


def adjusted_entry(open_price: Decimal, config: StrategyConfig) -> Decimal:
    return q6(open_price * (Decimal("1") + config.slippage_bps_per_side / Decimal("10000")))


def adjusted_exit(price: Decimal, config: StrategyConfig) -> Decimal:
    return q6(price * (Decimal("1") - config.slippage_bps_per_side / Decimal("10000")))


def position_quantity(entry_price: Decimal, stop_loss: Decimal, config: StrategyConfig) -> Decimal:
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        return Decimal("0")
    by_risk = (config.max_risk_per_trade / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)
    by_capital = (config.max_position_value / entry_price).to_integral_value(rounding=ROUND_FLOOR)
    return max(Decimal("0"), min(by_risk, by_capital))


def transaction_cost(entry_price: Decimal, exit_price: Decimal, quantity: Decimal, config: StrategyConfig) -> Decimal:
    turnover = (entry_price + exit_price) * quantity
    return q6(turnover * config.cost_bps_per_side / Decimal("10000"))


def simulate_trade(
    signal: Signal,
    entry_day: Candle,
    future_candles: Sequence[Candle],
    config: StrategyConfig,
) -> BacktestTrade | None:
    if signal.label not in {"paper_setup", "high_conviction_paper"}:
        return None
    if signal.stop_loss is None or signal.target is None:
        return None

    entry_price = adjusted_entry(entry_day.open, config)
    if entry_price <= signal.stop_loss:
        return None
    reward_risk = (signal.target - entry_price) / (entry_price - signal.stop_loss)
    if reward_risk < config.min_rr:
        return None

    quantity = position_quantity(entry_price, signal.stop_loss, config)
    if quantity < 1:
        return None

    max_days = max(1, config.max_holding_days)
    window = list(future_candles[:max_days])
    if not window:
        return None

    exit_price: Decimal | None = None
    exit_ts: datetime | None = None
    reason_exit: str | None = None
    for candle in window:
        # Conservative ambiguity handling: if both stop and target are touched in
        # the same candle, assume stop first.
        if candle.low <= signal.stop_loss:
            exit_price = adjusted_exit(signal.stop_loss, config)
            exit_ts = candle.ts
            reason_exit = "stop_hit"
            break
        if candle.high >= signal.target:
            exit_price = adjusted_exit(signal.target, config)
            exit_ts = candle.ts
            reason_exit = "target_hit"
            break

    if exit_price is None:
        last = window[-1]
        exit_price = adjusted_exit(last.close, config)
        exit_ts = last.ts
        reason_exit = "time_stop"

    gross_pnl = q6((exit_price - entry_price) * quantity)
    costs = transaction_cost(entry_price, exit_price, quantity, config)
    net_pnl = q6(gross_pnl - costs)
    return BacktestTrade(
        symbol=signal.symbol,
        side="BUY",
        entry_ts=entry_day.ts,
        exit_ts=exit_ts,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        reason_entry=f"{STRATEGY_NAME} {signal.label} score={signal.score}",
        reason_exit=reason_exit,
        score=signal.score,
        raw={
            "signal_ts": signal.ts.isoformat(),
            "signal_label": signal.label,
            "score": str(signal.score),
            "technical_score": str(signal.technical_score),
            "fundamental_score": str(signal.fundamental_score),
            "sentiment_score": str(signal.sentiment_score),
            "risk_score": str(signal.risk_score),
            "stop_loss": str(signal.stop_loss),
            "target": str(signal.target),
            "reasons": signal.reasons,
            "risks": signal.risks,
            "factors": signal.factors,
            "live_orders_enabled": False,
        },
    )


def compute_metrics(trades: Sequence[BacktestTrade], initial_capital: Decimal) -> dict[str, str | int]:
    total = len(trades)
    wins = [trade for trade in trades if trade.net_pnl > 0]
    losses = [trade for trade in trades if trade.net_pnl <= 0]
    net = q2(sum((trade.net_pnl for trade in trades), Decimal("0")))
    gross = q2(sum((trade.gross_pnl for trade in trades), Decimal("0")))
    win_rate = q2(Decimal(len(wins)) / Decimal(total) * Decimal("100")) if total else Decimal("0")
    avg_win = q2(sum((trade.net_pnl for trade in wins), Decimal("0")) / Decimal(len(wins))) if wins else Decimal("0")
    avg_loss = q2(sum((trade.net_pnl for trade in losses), Decimal("0")) / Decimal(len(losses))) if losses else Decimal("0")
    profit_factor_den = abs(sum((trade.net_pnl for trade in losses), Decimal("0")))
    profit_factor = q2(sum((trade.net_pnl for trade in wins), Decimal("0")) / profit_factor_den) if profit_factor_den else Decimal("0")

    equity = initial_capital
    peak = initial_capital
    max_drawdown = Decimal("0")
    for trade in sorted(trades, key=lambda item: item.exit_ts):
        equity += trade.net_pnl
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": str(win_rate),
        "gross_pnl": str(gross),
        "net_pnl": str(net),
        "ending_equity": str(q2(initial_capital + net)),
        "avg_win": str(avg_win),
        "avg_loss": str(avg_loss),
        "profit_factor": str(profit_factor),
        "max_drawdown": str(q2(max_drawdown)),
    }


def backtest_symbol(candles: Sequence[Candle], config: StrategyConfig) -> list[BacktestTrade]:
    trades: list[BacktestTrade] = []
    idx = 200
    while idx < len(candles) - 1:
        history = candles[: idx + 1]
        signal = evaluate_signal(history, config)
        if signal.label in {"paper_setup", "high_conviction_paper"}:
            trade = simulate_trade(signal, candles[idx + 1], candles[idx + 1 : idx + 1 + config.max_holding_days], config)
            if trade is not None:
                trades.append(trade)
                # Avoid overlapping trades in the same symbol.
                exit_index = next((j for j, candle in enumerate(candles) if candle.ts == trade.exit_ts), idx + 1)
                idx = max(idx + 1, exit_index + 1)
                continue
        idx += 1
    return trades


def apply_portfolio_limits(trades: Sequence[BacktestTrade], config: StrategyConfig) -> list[BacktestTrade]:
    """Apply small-capital safety constraints across the whole universe.

    The symbol-level simulator generates candidate trades independently. This
    pass turns those into a portfolio path with at most `max_open_positions` and
    no over-allocation of the configured capital. When multiple candidates start
    on the same day, higher score wins first.
    """
    accepted: list[BacktestTrade] = []
    open_trades: list[BacktestTrade] = []
    ordered = sorted(trades, key=lambda trade: (trade.entry_ts, -trade.score, trade.symbol))
    for trade in ordered:
        open_trades = [open_trade for open_trade in open_trades if open_trade.exit_ts > trade.entry_ts]
        reserved_capital = sum((open_trade.entry_price * open_trade.quantity for open_trade in open_trades), Decimal("0"))
        trade_capital = trade.entry_price * trade.quantity
        if len(open_trades) >= config.max_open_positions:
            continue
        if reserved_capital + trade_capital > config.initial_capital:
            continue
        accepted.append(trade)
        open_trades.append(trade)
    return sorted(accepted, key=lambda trade: (trade.entry_ts, trade.symbol))


def backtest_universe(
    candles_by_symbol: dict[str, list[Candle]],
    config: StrategyConfig,
    *,
    universe: str,
    resolution: str,
    warnings: list[str] | None = None,
) -> BacktestResult:
    all_candidates: list[BacktestTrade] = []
    for symbol in sorted(candles_by_symbol):
        candles = sorted(candles_by_symbol[symbol], key=lambda candle: candle.ts)
        if len(candles) >= 201:
            all_candidates.extend(backtest_symbol(candles, config))
    all_trades = apply_portfolio_limits(all_candidates, config)
    if candles_by_symbol:
        start_ts = min(candles[0].ts for candles in candles_by_symbol.values() if candles)
        end_ts = max(candles[-1].ts for candles in candles_by_symbol.values() if candles)
    else:
        now = datetime.now(timezone.utc)
        start_ts = now
        end_ts = now
    result_warnings = warnings or []
    result_warnings.append("Fundamental/sentiment historical evidence unavailable; neutral placeholders used for FTS_SWING_V1 v1.0.")
    return BacktestResult(
        strategy_name=STRATEGY_NAME,
        version=STRATEGY_VERSION,
        universe=universe,
        resolution=resolution,
        start_ts=start_ts,
        end_ts=end_ts,
        trades=all_trades,
        metrics=compute_metrics(all_trades, config.initial_capital),
        warnings=result_warnings,
    )


def load_watchlist(path: Path) -> list[str]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        symbols = []
        for row in reader:
            fyers_symbol = (row.get("fyers_symbol") or row.get("symbol") or "").strip()
            if fyers_symbol:
                symbols.append(fyers_symbol)
        return symbols


def fetch_candles_by_symbol(
    conn: psycopg.Connection,
    symbols: Sequence[str] | None,
    resolution: str,
    start: datetime | None,
    end: datetime | None,
    max_symbols: int | None,
) -> dict[str, list[Candle]]:
    params: list[object] = [resolution]
    filters = ["resolution = %s"]
    if symbols:
        filters.append("symbol = any(%s)")
        params.append(list(symbols))
    if start:
        filters.append("ts >= %s")
        params.append(start)
    if end:
        filters.append("ts <= %s")
        params.append(end)
    where = " and ".join(filters)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select symbol, resolution, ts, open, high, low, close, volume
            from market.candles
            where {where}
            order by symbol, ts
            """,
            params,
        )
        rows = cur.fetchall()

    candles_by_symbol: dict[str, list[Candle]] = {}
    for row in rows:
        if max_symbols is not None and row[0] not in candles_by_symbol and len(candles_by_symbol) >= max_symbols:
            continue
        candles_by_symbol.setdefault(row[0], []).append(
            Candle(
                symbol=row[0],
                resolution=row[1],
                ts=row[2],
                open=Decimal(row[3]),
                high=Decimal(row[4]),
                low=Decimal(row[5]),
                close=Decimal(row[6]),
                volume=row[7],
            )
        )
    return candles_by_symbol


def ensure_strategy_version(conn: psycopg.Connection, config: StrategyConfig) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into research.hypotheses(title, hypothesis, target_universe, timeframe, expected_edge, assumptions, status)
            values (
                'FTS Swing v1',
                'Liquid NSE equities with bullish technical structure, non-weak fundamentals, and non-negative sentiment/catalyst context can produce risk-controlled swing opportunities.',
                'NSE liquid equity watchlists',
                'daily swing, 1-5 holding days',
                'Positive expectancy after costs with strict stops, 2R target reference, and paper-first validation.',
                %s::jsonb,
                'ready_for_backtest'
            )
            on conflict do nothing
            returning hypothesis_id
            """,
            (json.dumps({"live_orders_enabled": False, "phase": "backtest"}),),
        )
        row = cur.fetchone()
        hypothesis_id = row[0] if row else None
        cur.execute(
            """
            insert into research.strategy_versions(strategy_name, version, code_path, config, parameters, assumptions, status, hypothesis_id)
            values (%s, %s, %s, %s::jsonb, %s::jsonb, %s, 'backtest', %s)
            on conflict(strategy_name, version) do update set
                code_path = excluded.code_path,
                config = excluded.config,
                parameters = excluded.parameters,
                assumptions = excluded.assumptions,
                status = excluded.status
            returning strategy_version_id
            """,
            (
                STRATEGY_NAME,
                STRATEGY_VERSION,
                "scripts/run_fts_swing_backtest.py",
                json.dumps(config.to_json()),
                json.dumps(config.to_json()),
                "Historical F/S evidence tables are not yet populated; v1.0 uses neutral F/S placeholders and real technical candles.",
                hypothesis_id,
            ),
        )
        strategy_version_id = cur.fetchone()[0]
    conn.commit()
    return strategy_version_id


def store_backtest_result(conn: psycopg.Connection, result: BacktestResult, config: StrategyConfig) -> int:
    strategy_version_id = ensure_strategy_version(conn, config)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into research.backtest_runs(
                strategy_version_id, universe, resolution, start_ts, end_ts, initial_capital,
                costs, slippage, metrics, status, notes, raw, finished_at
            )
            values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, 'success', %s, %s::jsonb, now())
            returning backtest_run_id
            """,
            (
                strategy_version_id,
                result.universe,
                result.resolution,
                result.start_ts,
                result.end_ts,
                config.initial_capital,
                json.dumps({"cost_bps_per_side": str(config.cost_bps_per_side)}),
                json.dumps({"slippage_bps_per_side": str(config.slippage_bps_per_side)}),
                json.dumps(result.metrics),
                "Research-only backtest. No orders placed. F/S placeholders used until evidence history is available.",
                json.dumps({"warnings": result.warnings, "config": config.to_json()}),
            ),
        )
        run_id = cur.fetchone()[0]
        for trade in result.trades:
            cur.execute(
                """
                insert into research.backtest_trades(
                    backtest_run_id, symbol, side, entry_ts, exit_ts, entry_price, exit_price,
                    quantity, gross_pnl, net_pnl, reason_entry, reason_exit, raw
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    run_id,
                    trade.symbol,
                    trade.side,
                    trade.entry_ts,
                    trade.exit_ts,
                    trade.entry_price,
                    trade.exit_price,
                    trade.quantity,
                    trade.gross_pnl,
                    trade.net_pnl,
                    trade.reason_entry,
                    trade.reason_exit,
                    json.dumps(trade.raw),
                ),
            )
    conn.commit()
    return run_id


def money(value: object) -> str:
    return f"₹{Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def render_report(result: BacktestResult) -> str:
    lines = [
        f"## {result.strategy_name} Backtest",
        f"Version: {result.version}",
        f"Backtest run ID: {result.backtest_run_id or 'not stored'}",
        f"Period: {result.start_ts.date()} to {result.end_ts.date()}",
        f"Universe: {result.universe}; Resolution: {result.resolution}",
        "Scope: research-only strategy backtest; not investment advice. No orders placed.",
        "",
        "## What this proves",
        "- Tests the technical core of FTS_SWING_V1 on stored FYERS candles.",
        "- Fundamental/sentiment components are wired into the strategy interface.",
        "- Historical fundamental/sentiment evidence is not populated yet, so v1 uses neutral placeholders and flags that limitation.",
        "",
        "## Metrics",
    ]
    metric_order = [
        "total_trades",
        "wins",
        "losses",
        "win_rate_pct",
        "gross_pnl",
        "net_pnl",
        "ending_equity",
        "avg_win",
        "avg_loss",
        "profit_factor",
        "max_drawdown",
    ]
    for key in metric_order:
        if key in result.metrics:
            value = result.metrics[key]
            if key in {"gross_pnl", "net_pnl", "ending_equity", "avg_win", "avg_loss", "max_drawdown"}:
                lines.append(f"- {key}: {money(value)}")
            elif key == "win_rate_pct":
                lines.append(f"- {key}: {value}%")
            else:
                lines.append(f"- {key}: {value}")

    if result.warnings:
        lines.extend(["", "## Warnings / limitations"])
        for warning in result.warnings:
            lines.append(f"- {warning}")

    lines.extend(["", "## Recent trades"])
    if not result.trades:
        lines.append("- No trades generated by the current rules.")
    else:
        for trade in sorted(result.trades, key=lambda item: item.exit_ts, reverse=True)[:10]:
            lines.extend(
                [
                    f"- {trade.symbol}",
                    f"  - Entry: {trade.entry_ts.date()} at {money(trade.entry_price)}; exit: {trade.exit_ts.date()} at {money(trade.exit_price)}",
                    f"  - Qty: {trade.quantity}; Net P&L: {money(trade.net_pnl)}; Exit: {trade.reason_exit}; Score: {trade.score}",
                ]
            )

    lines.extend(
        [
            "",
            "## Next build step",
            "- Populate historical/current fundamental and sentiment evidence tables, then rerun this same backtest with real F+S scores instead of neutral placeholders.",
            "- Only after backtest + paper validation should any live deployment be considered, and then only behind explicit approval.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_report(result: BacktestResult, output: Path | None) -> Path:
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output = DEFAULT_REPORT_DIR / f"fts_swing_v1_backtest_{stamp}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_report(result))
    return output


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Date must be YYYY-MM-DD: {value}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest FTS_SWING_V1 on stored FYERS candles. Research-only; no orders placed.")
    parser.add_argument("--watchlist", type=Path, help=f"Watchlist CSV; defaults to {DEFAULT_WATCHLIST}")
    parser.add_argument("--symbols", nargs="+", help="Explicit FYERS symbols, e.g. NSE:RELIANCE-EQ")
    parser.add_argument("--resolution", default="D")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("--max-symbols", type=int, help="Limit symbol count for smoke tests")
    parser.add_argument("--output", type=Path, help="Markdown report output path")
    parser.add_argument("--no-store", action="store_true", help="Do not write backtest run/trades into Postgres")
    parser.add_argument("--print", action="store_true", help="Print report text")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_symbols is not None and args.max_symbols <= 0:
        raise SystemExit("--max-symbols must be positive")
    config = StrategyConfig()
    start = parse_dt(args.start)
    end = parse_dt(args.end)
    symbols = args.symbols
    universe = "explicit-symbols" if symbols else "all-candles"
    if symbols is None and args.watchlist:
        symbols = load_watchlist(args.watchlist)
        universe = str(args.watchlist)
    elif symbols is None and DEFAULT_WATCHLIST.exists():
        symbols = load_watchlist(DEFAULT_WATCHLIST)
        universe = str(DEFAULT_WATCHLIST)

    with psycopg.connect(DEFAULT_DATABASE_URL) as conn:
        candles_by_symbol = fetch_candles_by_symbol(conn, symbols, args.resolution, start, end, args.max_symbols)
        result = backtest_universe(candles_by_symbol, config, universe=universe, resolution=args.resolution)
        run_id = None if args.no_store else store_backtest_result(conn, result, config)
        if run_id is not None:
            result = BacktestResult(
                strategy_name=result.strategy_name,
                version=result.version,
                universe=result.universe,
                resolution=result.resolution,
                start_ts=result.start_ts,
                end_ts=result.end_ts,
                trades=result.trades,
                metrics=result.metrics,
                warnings=result.warnings,
                backtest_run_id=run_id,
            )
    report_path = write_report(result, args.output)
    if args.print:
        print(render_report(result))
    print(f"FTS_SWING_V1 backtest complete. Report: {report_path}")
    print(f"Trades: {result.metrics.get('total_trades')} | Net P&L: {result.metrics.get('net_pnl')} | No orders placed.")


if __name__ == "__main__":
    main()
