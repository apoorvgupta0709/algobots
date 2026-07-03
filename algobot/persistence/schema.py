"""SQLAlchemy ORM models. Postgres in production (docker-compose), SQLite for dev/tests.

Tables are created via ``algobot.persistence.db.init_db()`` (create_all) — the
schema is greenfield; introduce Alembic when the first breaking migration lands.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class StrategyRow(Base):
    """Per-strategy runtime state: mode, param overrides, capital allocation."""
    __tablename__ = "strategies"

    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16), default="paper")  # off|backtest|paper|live
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    capital_alloc: Mapped[float] = mapped_column(Float, default=100_000.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow,
                                                    onupdate=dt.datetime.utcnow)


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    signal_type: Mapped[str] = mapped_column(String(16))
    instrument: Mapped[str] = mapped_column(String(64))
    reference_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    structure_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="emitted")  # emitted|rejected|executed
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16))                     # paper|live
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[int] = mapped_column(Integer)                        # 1 buy, -1 sell
    qty: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[int] = mapped_column(Integer, default=2)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    product_type: Mapped[str] = mapped_column(String(16), default="INTRADAY")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ts_placed: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    ts_filled: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    price: Mapped[float] = mapped_column(Float)
    qty: Mapped[int] = mapped_column(Integer)
    ts: Mapped[dt.datetime] = mapped_column(DateTime)


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int] = mapped_column(Integer)                          # signed
    avg_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    underlying: Mapped[str | None] = mapped_column(String(64), nullable=True)
    underlying_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    trail_anchor: Mapped[float | None] = mapped_column(Float, nullable=True)
    structure_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    structure_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    product_type: Mapped[str] = mapped_column(String(16), default="INTRADAY")
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|closed
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)


class TradeRow(Base):
    """Closed round trips — the journal. net_pnl is after the Indian cost stack."""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)
    entry_time: Mapped[dt.datetime] = mapped_column(DateTime)
    exit_time: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float)
    costs: Mapped[float] = mapped_column(Float)
    net_pnl: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(16))
    modeled_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    structure_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    equity: Mapped[float] = mapped_column(Float)
    day_pnl: Mapped[float] = mapped_column(Float, default=0.0)


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    start: Mapped[dt.date] = mapped_column(Date)
    end: Mapped[dt.date] = mapped_column(Date)
    data_source: Mapped[str] = mapped_column(String(16), default="real")  # real|synthetic|mixed
    cost_model_version: Mapped[str] = mapped_column(String(16), default="fy2026")
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class BacktestTradeRow(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    qty: Mapped[int] = mapped_column(Integer)
    entry_time: Mapped[dt.datetime] = mapped_column(DateTime)
    exit_time: Mapped[dt.datetime] = mapped_column(DateTime)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float)
    costs: Mapped[float] = mapped_column(Float)
    net_pnl: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(16))


class GateStatusRow(Base):
    """Automated paper-to-live gate evaluation (compendium §8.2)."""
    __tablename__ = "gate_status"

    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_trades_count: Mapped[int] = mapped_column(Integer, default=0)
    oos_backtest_months: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_fire_fidelity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    evaluated_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    promoted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RiskStateRow(Base):
    """One row per trading day: realized P&L vs caps + kill switch."""
    __tablename__ = "risk_state"

    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    realized_day_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_week_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    open_position_count: Mapped[int] = mapped_column(Integer, default=0)
    trades_today: Mapped[int] = mapped_column(Integer, default=0)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class QueryJobRow(Base):
    """Hermes job queue: POST /queries enqueues, worker answers, GET /queries/{id} reads."""
    __tablename__ = "query_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    payload_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="queued",
                                        index=True)  # queued|running|done|error
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class AuthTokenRow(Base):
    __tablename__ = "auth_tokens"

    broker: Mapped[str] = mapped_column(String(32), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class EventLogRow(Base):
    """Audit trail: engine starts, promotions, kill switch, auth failures, errors."""
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    source: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
