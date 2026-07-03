"""Tests for algobot.dashboard.data_access against a seeded sqlite tmp DB.

DATABASE_URL is pointed at a per-test sqlite file BEFORE the cached settings /
engine are (re)built, and all lru_caches are cleared so each test gets a fresh
database. Streamlit rendering is deliberately not tested.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from algobot.core import clock
from algobot.core import config as core_config
from algobot.persistence import db
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

from algobot.dashboard import data_access as da

TODAY = clock.now_ist().date()


def _reset_caches() -> None:
    core_config.settings.cache_clear()
    if db.get_engine.cache_info().currsize:
        db.get_engine().dispose()
    db.get_engine.cache_clear()
    db.get_sessionmaker.cache_clear()


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/dash_test.db")
    _reset_caches()
    db.init_db()
    yield
    _reset_caches()


def _ts(day: dt.date, hour: int = 10, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute))


def _trade(**kw) -> TradeRow:
    base = dict(
        strategy_id="id01_orb", mode="paper", symbol="NSE:RELIANCE-EQ",
        direction="long", qty=10,
        entry_time=_ts(TODAY, 9, 30), exit_time=_ts(TODAY, 10, 30),
        entry_price=100.0, exit_price=110.0,
        gross_pnl=100.0, costs=10.0, net_pnl=90.0, exit_reason="tp",
    )
    base.update(kw)
    return TradeRow(**base)


def _seed(rows) -> None:
    with db.session_scope() as s:
        s.add_all(rows)


# ------------------------------------------------------------- empty-DB safety

def test_empty_db_all_functions_return_correct_columns(fresh_db):
    assert list(da.open_positions().columns) == da.POSITION_COLUMNS
    assert list(da.trades().columns) == da.TRADE_COLUMNS
    assert list(da.todays_pnl_by_strategy().columns) == da.TODAYS_PNL_COLUMNS
    assert list(da.equity_curves().columns) == da.EQUITY_COLUMNS
    assert list(da.gate_details().columns) == da.GATE_COLUMNS
    assert list(da.backtest_runs().columns) == da.BACKTEST_COLUMNS
    assert list(da.events().columns) == da.EVENT_COLUMNS
    for df in (da.open_positions(), da.trades(), da.todays_pnl_by_strategy(),
               da.equity_curves(), da.gate_details(), da.backtest_runs(),
               da.events()):
        assert isinstance(df, pd.DataFrame)
        assert df.empty


def test_empty_db_strategies_overview_has_registry_rows(fresh_db):
    df = da.strategies_overview()
    assert list(df.columns) == da.OVERVIEW_COLUMNS
    # registry strategies appear even with an empty DB, in mode 'off'
    if not df.empty:
        assert (df["mode"] == "off").all()
        assert (~df["eligible"]).all()


def test_empty_db_risk_today_defaults_and_caps(fresh_db):
    risk = da.risk_today()
    assert risk["date"] == TODAY
    assert risk["realized_day_pnl"] == 0.0
    assert risk["kill_switch"] is False
    assert risk["kill_reason"] is None
    cfg = core_config.settings()
    cap = cfg["capital"]
    assert risk["daily_loss_cap"] == pytest.approx(
        cap * cfg["risk"]["daily_loss_cap_pct"] / 100)
    assert risk["weekly_loss_cap"] == pytest.approx(
        cap * cfg["risk"]["weekly_loss_cap_pct"] / 100)
    assert risk["max_concurrent_positions"] == cfg["risk"]["max_concurrent_positions"]
    assert risk["max_trades_per_day"] == cfg["risk"]["max_trades_per_day"]


# ---------------------------------------------------------------- positions

def test_open_positions_filters_and_unrealized_fallback(fresh_db):
    _seed([
        PositionRow(strategy_id="s1", mode="paper", symbol="A", qty=10,
                    avg_price=100.0, opened_at=_ts(TODAY, 9, 20), status="open",
                    last_price=105.0, unrealized_pnl=None),
        PositionRow(strategy_id="s1", mode="live", symbol="B", qty=-5,
                    avg_price=200.0, opened_at=_ts(TODAY, 9, 25), status="open",
                    last_price=190.0, unrealized_pnl=77.0),
        PositionRow(strategy_id="s1", mode="paper", symbol="C", qty=1,
                    avg_price=50.0, opened_at=_ts(TODAY, 9, 30), status="closed"),
    ])
    df = da.open_positions()
    assert len(df) == 2  # closed row excluded
    a = df[df["symbol"] == "A"].iloc[0]
    assert a["unrealized_pnl"] == pytest.approx(50.0)  # (105-100)*10 fallback
    b = df[df["symbol"] == "B"].iloc[0]
    assert b["unrealized_pnl"] == pytest.approx(77.0)  # stored value wins
    live = da.open_positions(mode="live")
    assert list(live["symbol"]) == ["B"]


# ------------------------------------------------------------------- trades

def test_trades_filters_mode_strategy_and_days(fresh_db):
    old_day = TODAY - dt.timedelta(days=45)
    _seed([
        _trade(net_pnl=90.0),
        _trade(strategy_id="sw04_supertrend_adx", mode="live", net_pnl=-40.0),
        _trade(entry_time=_ts(old_day, 9, 30), exit_time=_ts(old_day, 10, 0),
               net_pnl=999.0),
    ])
    assert len(da.trades(days=None)) == 3
    assert len(da.trades(days=30)) == 2          # 45-day-old trade excluded
    assert len(da.trades(mode="live")) == 1
    assert len(da.trades(strategy_id="id01_orb", days=None)) == 2
    df = da.trades(mode="live", strategy_id="sw04_supertrend_adx")
    assert len(df) == 1 and df.iloc[0]["net_pnl"] == pytest.approx(-40.0)


def test_todays_pnl_by_strategy_aggregation(fresh_db):
    yesterday = TODAY - dt.timedelta(days=1)
    _seed([
        _trade(net_pnl=90.0, gross_pnl=100.0),
        _trade(net_pnl=-30.0, gross_pnl=-25.0, exit_reason="sl"),
        _trade(strategy_id="sw04_supertrend_adx", mode="live", net_pnl=55.0,
               gross_pnl=60.0),
        _trade(exit_time=_ts(yesterday, 14, 0), net_pnl=1000.0),  # not today
    ])
    df = da.todays_pnl_by_strategy()
    assert list(df.columns) == da.TODAYS_PNL_COLUMNS
    assert len(df) == 2
    orb = df[df["strategy_id"] == "id01_orb"].iloc[0]
    assert orb["net_pnl"] == pytest.approx(60.0)
    assert orb["gross_pnl"] == pytest.approx(75.0)
    assert orb["trades"] == 2
    live = df[df["strategy_id"] == "sw04_supertrend_adx"].iloc[0]
    assert live["mode"] == "live"
    assert live["net_pnl"] == pytest.approx(55.0)


# ------------------------------------------------------------- equity curves

def test_equity_curves_shape_and_downsampling(fresh_db):
    base = _ts(TODAY - dt.timedelta(days=5), 9, 15)
    rows = [EquitySnapshotRow(ts=base + dt.timedelta(minutes=i),
                              strategy_id="s1", mode="paper",
                              equity=100_000 + i, day_pnl=float(i))
            for i in range(50)]
    rows += [EquitySnapshotRow(ts=base + dt.timedelta(minutes=i),
                               strategy_id="s2", mode="live",
                               equity=200_000 + i, day_pnl=0.0)
             for i in range(5)]
    _seed(rows)

    df = da.equity_curves()
    assert list(df.columns) == da.EQUITY_COLUMNS
    assert len(df) == 55  # under the points cap: everything comes back

    small = da.equity_curves(points=10)
    s1 = small[small["strategy_id"] == "s1"]
    s2 = small[small["strategy_id"] == "s2"]
    assert len(s1) == 10        # downsampled per curve
    assert len(s2) == 5         # short curve untouched
    # endpoints preserved and order maintained
    assert s1["equity"].iloc[0] == 100_000
    assert s1["equity"].iloc[-1] == 100_049
    assert small["ts"].is_monotonic_increasing

    only_s1 = da.equity_curves(strategy_id="s1")
    assert set(only_s1["strategy_id"]) == {"s1"}


# -------------------------------------------------------- overview / gates

def test_strategies_overview_joins_db_registry_and_gates(fresh_db):
    _seed([
        StrategyRow(strategy_id="id01_orb", category="intraday", mode="live",
                    capital_alloc=250_000.0, enabled=True),
        StrategyRow(strategy_id="custom_db_only", category="swing",
                    mode="paper", capital_alloc=50_000.0, enabled=False),
        GateStatusRow(strategy_id="id01_orb", paper_trades_count=80,
                      profit_factor=1.6, eligible=True,
                      evaluated_at=_ts(TODAY, 8, 0)),
    ])
    df = da.strategies_overview()
    assert list(df.columns) == da.OVERVIEW_COLUMNS

    orb = df[df["strategy_id"] == "id01_orb"].iloc[0]
    assert orb["mode"] == "live"
    assert orb["capital_alloc"] == pytest.approx(250_000.0)
    assert bool(orb["eligible"]) is True
    assert orb["name"] != "id01_orb"       # human name from registry meta
    assert orb["timeframe"] != ""          # meta joined

    dbonly = df[df["strategy_id"] == "custom_db_only"].iloc[0]
    assert dbonly["mode"] == "paper"
    assert dbonly["name"] == "custom_db_only"   # no registry meta -> id
    assert bool(dbonly["eligible"]) is False

    # registry strategy with no DB row shows up as mode 'off'
    reg_only = df[df["strategy_id"] == "sw04_supertrend_adx"]
    assert len(reg_only) == 1 and reg_only.iloc[0]["mode"] == "off"


def test_gate_details_join_and_columns(fresh_db):
    _seed([
        StrategyRow(strategy_id="id01_orb", category="intraday", mode="paper"),
        GateStatusRow(strategy_id="id01_orb", paper_trades_count=42,
                      oos_backtest_months=7.0, profit_factor=1.4,
                      max_drawdown_pct=9.5, stop_fire_fidelity_pct=0.3,
                      eligible=False, detail_json={"why": "not enough trades"}),
    ])
    df = da.gate_details()
    assert list(df.columns) == da.GATE_COLUMNS
    row = df.iloc[0]
    assert row["mode"] == "paper"
    assert row["paper_trades_count"] == 42
    assert row["detail_json"] == {"why": "not enough trades"}


# ------------------------------------------------------------ risk / events

def test_risk_today_reads_todays_row(fresh_db):
    _seed([RiskStateRow(date=TODAY, realized_day_pnl=-4200.0,
                        realized_week_pnl=-6100.0, open_position_count=2,
                        trades_today=7, kill_switch=True, kill_reason="manual")])
    risk = da.risk_today()
    assert risk["realized_day_pnl"] == pytest.approx(-4200.0)
    assert risk["realized_week_pnl"] == pytest.approx(-6100.0)
    assert risk["open_position_count"] == 2
    assert risk["trades_today"] == 7
    assert risk["kill_switch"] is True
    assert risk["kill_reason"] == "manual"


def test_backtest_runs_metric_extraction_and_filter(fresh_db):
    _seed([
        BacktestRunRow(strategy_id="id01_orb", start=dt.date(2025, 1, 1),
                       end=dt.date(2025, 6, 30), data_source="real",
                       metrics_json={"profit_factor": 1.7, "max_drawdown_pct": 8.2,
                                     "sharpe": 1.1, "trades": 120, "net_pnl": 45000},
                       created_at=_ts(TODAY, 8, 0)),
        BacktestRunRow(strategy_id="sw04_supertrend_adx", start=dt.date(2025, 1, 1),
                       end=dt.date(2025, 3, 31), data_source="synthetic",
                       metrics_json={"pf": 0.9, "n_trades": 33},
                       created_at=_ts(TODAY, 9, 0)),
    ])
    df = da.backtest_runs()
    assert list(df.columns) == da.BACKTEST_COLUMNS
    assert len(df) == 2
    assert df.iloc[0]["created_at"] >= df.iloc[1]["created_at"]  # newest first
    orb = df[df["strategy_id"] == "id01_orb"].iloc[0]
    assert orb["profit_factor"] == pytest.approx(1.7)
    assert orb["trades"] == 120
    syn = df[df["strategy_id"] == "sw04_supertrend_adx"].iloc[0]
    assert syn["profit_factor"] == pytest.approx(0.9)   # 'pf' alias
    assert syn["trades"] == 33                          # 'n_trades' alias
    assert len(da.backtest_runs("id01_orb")) == 1


def test_events_limit_and_order(fresh_db):
    _seed([EventLogRow(ts=_ts(TODAY, 8, i), level="info" if i % 2 else "error",
                       source="engine", message=f"event {i}")
           for i in range(10)])
    df = da.events(limit=4)
    assert list(df.columns) == da.EVENT_COLUMNS
    assert len(df) == 4
    assert df.iloc[0]["message"] == "event 9"  # newest first
    assert df["ts"].is_monotonic_decreasing
