from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import run_fts_swing_backtest as fts


def make_candles(count: int = 230, *, start_price: Decimal = Decimal("100")) -> list[fts.Candle]:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    candles: list[fts.Candle] = []
    for idx in range(count):
        # Rising but choppy series: constructive trend without impossible RSI=100.
        close = start_price + Decimal(idx) * Decimal("0.25") + (Decimal(idx % 5) - Decimal("2")) * Decimal("1.20")
        open_price = close - Decimal("0.10")
        candles.append(
            fts.Candle(
                symbol="NSE:GOOD-EQ",
                resolution="D",
                ts=start + timedelta(days=idx),
                open=open_price,
                high=close + Decimal("1.00"),
                low=close - Decimal("1.00"),
                close=close,
                volume=100_000 + idx * 100,
            )
        )
    return candles


def test_evaluate_signal_accepts_constructive_fts_setup_with_neutral_evidence() -> None:
    config = fts.StrategyConfig()
    candles = make_candles()

    signal = fts.evaluate_signal(candles[:220], config)

    assert signal.label in {"paper_setup", "high_conviction_paper"}
    assert signal.score >= Decimal("65")
    assert signal.technical_score > Decimal("0")
    assert signal.fundamental_score == config.neutral_fundamental_score
    assert signal.sentiment_score == config.neutral_sentiment_score
    assert signal.entry_trigger == candles[219].close
    assert signal.stop_loss is not None
    assert signal.target is not None
    assert any("Technical" in reason for reason in signal.reasons)
    assert any("Fundamental" in reason for reason in signal.reasons)
    assert any("Sentiment" in reason for reason in signal.reasons)


def test_negative_fundamental_or_sentiment_evidence_forces_needs_review() -> None:
    config = fts.StrategyConfig()
    candles = make_candles()
    evidence = fts.EvidenceSnapshot(fundamental_label="weak", sentiment_label="negative")

    signal = fts.evaluate_signal(candles[:220], config, evidence=evidence)

    assert signal.label == "needs_review"
    assert any("fundamental" in risk.lower() for risk in signal.risks)
    assert any("sentiment" in risk.lower() for risk in signal.risks)


def test_simulate_trade_hits_target_before_time_stop() -> None:
    config = fts.StrategyConfig(max_holding_days=5)
    candles = make_candles()
    signal = fts.evaluate_signal(candles[:220], config)
    raw_entry_day = candles[220]
    entry_day = fts.Candle(
        symbol=raw_entry_day.symbol,
        resolution=raw_entry_day.resolution,
        ts=raw_entry_day.ts,
        open=signal.entry_trigger + Decimal("0.20"),
        high=signal.entry_trigger + Decimal("0.80"),
        low=signal.entry_trigger - Decimal("0.10"),
        close=signal.entry_trigger + Decimal("0.30"),
        volume=raw_entry_day.volume,
    )
    future = [entry_day] + candles[221:225]
    target_day = future[1]
    future[1] = fts.Candle(
        symbol=target_day.symbol,
        resolution=target_day.resolution,
        ts=target_day.ts,
        open=target_day.open,
        high=signal.target + Decimal("0.50"),
        low=signal.stop_loss + Decimal("0.50"),
        close=target_day.close,
        volume=target_day.volume,
    )

    trade = fts.simulate_trade(signal, entry_day, future, config)

    assert trade is not None
    assert trade.reason_exit == "target_hit"
    assert trade.exit_price == fts.adjusted_exit(signal.target, config)
    assert trade.net_pnl > 0
    assert trade.quantity >= Decimal("1")


def trade_candidate(symbol: str, *, entry_ts: datetime, exit_ts: datetime, score: Decimal = Decimal("70")) -> fts.BacktestTrade:
    return fts.BacktestTrade(
        symbol=symbol,
        side="BUY",
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_price=Decimal("100"),
        exit_price=Decimal("105"),
        quantity=Decimal("10"),
        gross_pnl=Decimal("50"),
        net_pnl=Decimal("49"),
        reason_entry="unit candidate",
        reason_exit="time_stop",
        score=score,
        raw={},
    )


def test_portfolio_limits_cap_concurrent_positions_and_prefers_higher_score() -> None:
    config = fts.StrategyConfig(max_open_positions=2, initial_capital=Decimal("5000"))
    same_day = datetime(2025, 6, 1, tzinfo=timezone.utc)
    exit_day = datetime(2025, 6, 3, tzinfo=timezone.utc)
    candidates = [
        trade_candidate("NSE:LOW-EQ", entry_ts=same_day, exit_ts=exit_day, score=Decimal("65")),
        trade_candidate("NSE:HIGH-EQ", entry_ts=same_day, exit_ts=exit_day, score=Decimal("80")),
        trade_candidate("NSE:MID-EQ", entry_ts=same_day, exit_ts=exit_day, score=Decimal("70")),
    ]

    accepted = fts.apply_portfolio_limits(candidates, config)

    assert [trade.symbol for trade in accepted] == ["NSE:HIGH-EQ", "NSE:MID-EQ"]


def test_backtest_report_is_research_only_and_mentions_fts_limitations() -> None:
    result = fts.BacktestResult(
        strategy_name="FTS_SWING_V1",
        version="1.0",
        universe="unit-test",
        resolution="D",
        start_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_ts=datetime(2025, 2, 1, tzinfo=timezone.utc),
        trades=[],
        metrics={"total_trades": 0, "net_pnl": "0", "win_rate": "0", "max_drawdown": "0"},
        warnings=["Fundamental/sentiment historical evidence unavailable; neutral placeholders used."],
        backtest_run_id=None,
    )

    text = fts.render_report(result)

    assert "FTS_SWING_V1 Backtest" in text
    assert "Fundamental/sentiment" in text
    assert "No orders placed" in text
    assert "research-only" in text
    assert "place order" not in text.lower().replace("no orders placed", "")
