"""Engine service tests: lifecycle state machine, paper-to-live gate,
StrategyRunner scan/monitor/snapshot resilience.

No network. A throwaway sqlite DATABASE_URL is set BEFORE any algobot import;
a dummy strategy class is injected straight into the registry (saved/restored
around every test) so nothing here depends on the shipped strategy files.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="algobot-engine-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"

import pandas as pd
import pytest

from algobot.core import config as config_mod

config_mod.settings.cache_clear()
config_mod.gate_config.cache_clear()

from algobot.persistence import db as db_mod

db_mod.get_engine.cache_clear()
db_mod.get_sessionmaker.cache_clear()

from algobot.broker.paper import PaperBroker
from algobot.core import registry as registry_mod
from algobot.core.clock import now_ist
from algobot.core.enums import Category, Mode, SignalType, Timeframe
from algobot.core.exceptions import GateError
from algobot.core.models import Signal
from algobot.core.strategy import (
    SCAN_EVERY_5MIN,
    StrategyBase,
    StrategyContext,
    StrategyMeta,
)
from algobot.data.feed import DataFeed
from algobot.engine import gate, lifecycle
from algobot.engine.runner import StrategyRunner
from algobot.execution.order_manager import OrderManager
from algobot.execution.risk import RiskEngine
from algobot.persistence.db import session_scope
from algobot.persistence.schema import (
    BacktestRunRow,
    Base,
    EquitySnapshotRow,
    EventLogRow,
    GateStatusRow,
    PositionRow,
    StrategyRow,
    TradeRow,
)

SYMBOL = "NSE:TESTSYM-EQ"
PRICE = 100.0


# --------------------------------------------------------------------------- dummies
class DummyStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="zz98_dummy",
        name="Dummy Long",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=[SYMBOL],
        warmup_bars=5,
        params={"stop_pct": 1.0},
    )

    def generate_signals(self, data, ctx: StrategyContext):
        if ctx.has_open_position:
            return []
        df = data[SYMBOL]
        px = float(df["close"].iloc[-1])
        return [Signal(
            strategy_id=self.strategy_id,
            signal_type=SignalType.ENTRY_LONG,
            instrument=SYMBOL,
            timestamp=ctx.now,
            reference_price=px,
            stop_loss=px * (1 - self.params["stop_pct"] / 100.0),
            reason="test entry",
        )]


class BoomStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="zz99_boom",
        name="Always Raises",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=[SYMBOL],
        warmup_bars=5,
    )

    def generate_signals(self, data, ctx):
        raise RuntimeError("boom: intentional test failure")


class FakeFeed(DataFeed):
    """50 flat 5-min bars ending now; constant quotes. No network."""

    def __init__(self, price: float = PRICE):
        self.price = price

    def get_candles(self, symbol, timeframe, start, end):
        idx = pd.date_range(end=now_ist().replace(second=0, microsecond=0),
                            periods=50, freq="5min", tz="Asia/Kolkata", name="ts")
        return pd.DataFrame(
            {"open": self.price, "high": self.price * 1.001,
             "low": self.price * 0.999, "close": self.price, "volume": 1000.0},
            index=idx)

    def get_quotes(self, symbols):
        return {s: self.price for s in symbols}


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def clean_db():
    db_mod.init_db()
    with session_scope() as s:
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
    yield


@pytest.fixture(autouse=True)
def sandbox_registry():
    saved = dict(registry_mod._REGISTRY)
    saved_loaded = registry_mod._LOADED
    registry_mod._REGISTRY.clear()
    registry_mod._REGISTRY.update({
        DummyStrategy.meta.strategy_id: DummyStrategy,
        BoomStrategy.meta.strategy_id: BoomStrategy,
    })
    registry_mod._LOADED = True
    yield
    registry_mod._REGISTRY.clear()
    registry_mod._REGISTRY.update(saved)
    registry_mod._LOADED = saved_loaded


def make_runner(feed: DataFeed | None = None) -> StrategyRunner:
    feed = feed or FakeFeed()
    risk = RiskEngine()
    brokers = {Mode.PAPER: PaperBroker(quote_fn=feed.get_quotes)}
    return StrategyRunner(feed, OrderManager(brokers, risk), risk)


def seed_paper_trades(strategy_id: str, n: int, win: float = 800.0,
                      loss: float = -500.0) -> None:
    """Alternating win/loss paper trades -> PF ~ win/|loss|, tiny drawdown."""
    t0 = dt.datetime.combine(now_ist().date(), dt.time(10, 0))
    with session_scope() as s:
        for i in range(n):
            pnl = win if i % 2 == 0 else loss
            s.add(TradeRow(
                strategy_id=strategy_id, mode="paper", symbol=SYMBOL,
                direction="long", qty=10,
                entry_time=t0 - dt.timedelta(days=n - i, hours=1),
                exit_time=t0 - dt.timedelta(days=n - i),
                entry_price=100.0, exit_price=100.0 + pnl / 10,
                gross_pnl=pnl, costs=0.0, net_pnl=pnl, exit_reason="tp"))


# =========================================================================== lifecycle
class TestLifecycle:
    def test_sync_inserts_rows_for_registered_strategies(self):
        inserted = lifecycle.sync_config_to_db()
        assert set(inserted) == {"zz98_dummy", "zz99_boom"}
        with session_scope() as s:
            row = s.get(StrategyRow, "zz98_dummy")
            assert row is not None
            assert row.mode == "paper"            # yaml defaults
            assert row.capital_alloc == 100000    # yaml defaults
            assert row.category == "intraday"
            assert row.enabled is True

    def test_sync_never_overwrites_existing_rows(self):
        lifecycle.sync_config_to_db()
        with session_scope() as s:
            s.get(StrategyRow, "zz98_dummy").mode = "off"
            s.get(StrategyRow, "zz98_dummy").capital_alloc = 42.0
        assert lifecycle.sync_config_to_db() == []   # idempotent
        with session_scope() as s:
            row = s.get(StrategyRow, "zz98_dummy")
            assert row.mode == "off"
            assert row.capital_alloc == 42.0

    def test_get_active_filters_and_merges_params(self):
        lifecycle.sync_config_to_db()
        with session_scope() as s:
            s.get(StrategyRow, "zz98_dummy").params_json = {"stop_pct": 2.5}
            s.get(StrategyRow, "zz99_boom").mode = "off"
        active = lifecycle.get_active()
        assert [row.strategy_id for _, row in active] == ["zz98_dummy"]
        strategy, _ = active[0]
        assert strategy.params["stop_pct"] == 2.5   # DB override over meta default

    def test_set_mode_live_requires_eligible_gate(self):
        lifecycle.sync_config_to_db()
        with pytest.raises(GateError):
            lifecycle.set_mode("zz98_dummy", Mode.LIVE)

    def test_set_mode_live_with_force_refused_while_fuse_closed(self):
        # force bypasses the gate but can never bypass the live-orders fuse.
        lifecycle.sync_config_to_db()
        with pytest.raises(GateError, match="fuse"):
            lifecycle.set_mode("zz98_dummy", Mode.LIVE, actor="tester",
                               force=True)

    def test_set_mode_live_with_force(self, monkeypatch):
        monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", "true")
        lifecycle.sync_config_to_db()
        row = lifecycle.set_mode("zz98_dummy", Mode.LIVE, actor="tester",
                                 force=True)
        assert row.mode == "live"
        with session_scope() as s:
            gate_row = s.get(GateStatusRow, "zz98_dummy")
            assert gate_row.promoted_by == "tester"
            assert gate_row.promoted_at is not None
            events = s.query(EventLogRow).filter_by(source="lifecycle").all()
            assert any("live" in e.message for e in events)

    def test_set_mode_live_eligible_gate_refused_while_fuse_closed(self):
        lifecycle.sync_config_to_db()
        with session_scope() as s:
            s.add(GateStatusRow(strategy_id="zz98_dummy", eligible=True))
        with pytest.raises(GateError, match="fuse"):
            lifecycle.set_mode("zz98_dummy", Mode.LIVE, actor="api")

    def test_set_mode_live_with_eligible_gate(self, monkeypatch):
        monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", "true")
        lifecycle.sync_config_to_db()
        with session_scope() as s:
            s.add(GateStatusRow(strategy_id="zz98_dummy", eligible=True))
        row = lifecycle.set_mode("zz98_dummy", Mode.LIVE, actor="api")
        assert row.mode == "live"

    def test_set_mode_unknown_strategy(self):
        with pytest.raises(KeyError):
            lifecycle.set_mode("nope", Mode.PAPER)


# =========================================================================== gate
class TestGate:
    def test_eligible_with_enough_profitable_paper_trades(self):
        lifecycle.sync_config_to_db()
        seed_paper_trades("zz98_dummy", 70)    # PF 1.6, dd well under 15%
        row = gate.evaluate("zz98_dummy")
        assert row.paper_trades_count == 70
        assert row.profit_factor == pytest.approx(1.6, abs=0.01)
        assert row.max_drawdown_pct < 15.0
        assert row.stop_fire_fidelity_pct is None   # no modeled exits yet
        assert row.eligible is True
        assert row.detail_json["profit_factor"]["pass"] is True
        # gaining eligibility is journalled
        with session_scope() as s:
            assert s.query(EventLogRow).filter_by(source="gate").count() == 1

    def test_not_eligible_with_few_trades(self):
        lifecycle.sync_config_to_db()
        seed_paper_trades("zz98_dummy", 5)
        row = gate.evaluate("zz98_dummy")
        assert row.eligible is False
        detail = row.detail_json
        assert detail["paper_trades"]["value"] == 5
        assert detail["paper_trades"]["pass"] is False
        assert detail["sample"]["pass"] is False
        assert detail["profit_factor"]["pass"] is True  # edge fine, sample not

    def test_oos_backtest_months_substitute_for_trades(self):
        lifecycle.sync_config_to_db()
        seed_paper_trades("zz98_dummy", 10)    # too few paper trades alone
        with session_scope() as s:
            s.add(BacktestRunRow(
                strategy_id="zz98_dummy",
                start=dt.date(2025, 1, 1), end=dt.date(2025, 12, 31),
                data_source="real"))
        row = gate.evaluate("zz98_dummy")
        assert row.oos_backtest_months == pytest.approx(12.1, abs=0.1)
        assert row.eligible is True

    def test_synthetic_backtests_are_discounted(self):
        lifecycle.sync_config_to_db()
        with session_scope() as s:
            s.add(BacktestRunRow(
                strategy_id="zz98_dummy",
                start=dt.date(2025, 7, 1), end=dt.date(2025, 12, 31),
                data_source="synthetic"))
        row = gate.evaluate("zz98_dummy")
        # ~6 months * 0.5 discount ~= 3 -> below the 6-month bar
        assert row.oos_backtest_months < 6.0
        assert row.detail_json["oos_backtest_months"]["weight"] == 0.5
        assert row.eligible is False

    def test_bad_stop_fidelity_blocks(self):
        lifecycle.sync_config_to_db()
        seed_paper_trades("zz98_dummy", 70)
        with session_scope() as s:
            trade = s.query(TradeRow).first()
            trade.modeled_exit_price = 100.0
            trade.exit_price = 110.0            # 10% miss >> 0.5% tolerance
        row = gate.evaluate("zz98_dummy")
        assert row.stop_fire_fidelity_pct == pytest.approx(10.0)
        assert row.eligible is False

    def test_evaluate_all(self):
        lifecycle.sync_config_to_db()
        seed_paper_trades("zz98_dummy", 70)
        results = gate.evaluate_all()
        assert results == {"zz98_dummy": True, "zz99_boom": False}


# =========================================================================== runner
class TestRunner:
    def test_scan_creates_position_and_survives_broken_strategy(self):
        lifecycle.sync_config_to_db()
        runner = make_runner()
        runner.scan(SCAN_EVERY_5MIN)   # BoomStrategy raises inside this scan

        with session_scope() as s:
            positions = (s.query(PositionRow)
                         .filter_by(strategy_id="zz98_dummy", mode="paper",
                                    status="open").all())
            assert len(positions) == 1
            pos = positions[0]
            assert pos.symbol == SYMBOL
            assert pos.qty > 0
            assert pos.stop_loss == pytest.approx(PRICE * 0.99)
            # the broken strategy's failure was journalled, not raised
            errors = (s.query(EventLogRow)
                      .filter_by(source="engine", level="error").all())
            assert any("zz99_boom" in e.message for e in errors)

    def test_scan_skips_strategies_on_other_schedules(self):
        lifecycle.sync_config_to_db()
        runner = make_runner()
        runner.scan("eod")
        with session_scope() as s:
            assert s.query(PositionRow).count() == 0

    def test_scan_no_reentry_with_open_position(self):
        lifecycle.sync_config_to_db()
        runner = make_runner()
        runner.scan(SCAN_EVERY_5MIN)
        runner.scan(SCAN_EVERY_5MIN)   # ctx.open_positions blocks a second entry
        with session_scope() as s:
            assert (s.query(PositionRow)
                    .filter_by(strategy_id="zz98_dummy", status="open")
                    .count()) == 1

    def test_scan_survives_feed_failure(self):
        class DeadFeed(FakeFeed):
            def get_candles(self, *a, **k):
                raise RuntimeError("feed down")

        lifecycle.sync_config_to_db()
        runner = make_runner(DeadFeed())
        runner.scan(SCAN_EVERY_5MIN)   # must not raise
        with session_scope() as s:
            assert s.query(PositionRow).count() == 0

    def test_scan_drops_stale_candles_and_takes_no_trade(self):
        class StaleFeed(FakeFeed):
            def get_candles(self, symbol, timeframe, start, end):
                # Bars end 3 hours ago -> far beyond the freshness cutoff.
                idx = pd.date_range(
                    end=now_ist().replace(second=0, microsecond=0)
                    - dt.timedelta(hours=3),
                    periods=50, freq="5min", tz="Asia/Kolkata", name="ts")
                return pd.DataFrame(
                    {"open": self.price, "high": self.price,
                     "low": self.price, "close": self.price, "volume": 1000.0},
                    index=idx)

        lifecycle.sync_config_to_db()
        runner = make_runner(StaleFeed())
        runner.scan(SCAN_EVERY_5MIN)   # must not fire signals off stale data
        with session_scope() as s:
            assert s.query(PositionRow).count() == 0
            warns = (s.query(EventLogRow)
                     .filter_by(source="engine", level="warn").all())
            assert any("stale" in e.message for e in warns)

    def test_monitor_tick_marks_positions(self):
        lifecycle.sync_config_to_db()
        runner = make_runner()
        runner.scan(SCAN_EVERY_5MIN)
        runner.monitor_tick()
        with session_scope() as s:
            pos = (s.query(PositionRow)
                   .filter_by(strategy_id="zz98_dummy", status="open").one())
            assert pos.last_price == pytest.approx(PRICE)
            assert pos.unrealized_pnl is not None

    def test_monitor_tick_fires_stop(self):
        lifecycle.sync_config_to_db()
        feed = FakeFeed()
        runner = make_runner(feed)
        runner.scan(SCAN_EVERY_5MIN)
        feed.price = PRICE * 0.95      # crash through the 1% stop
        runner.monitor_tick()
        with session_scope() as s:
            assert (s.query(PositionRow)
                    .filter_by(strategy_id="zz98_dummy", status="open")
                    .count()) == 0
            trade = (s.query(TradeRow)
                     .filter_by(strategy_id="zz98_dummy", mode="paper").one())
            assert trade.exit_reason == "sl"
            assert trade.modeled_exit_price == pytest.approx(PRICE * 0.99)

    def test_snapshot_equity_writes_rows(self):
        lifecycle.sync_config_to_db()
        runner = make_runner()
        runner.scan(SCAN_EVERY_5MIN)
        runner.monitor_tick()          # refresh marks first
        written = runner.snapshot_equity()
        assert written == 2            # both active paper strategies
        with session_scope() as s:
            snaps = {r.strategy_id: r for r in s.query(EquitySnapshotRow).all()}
            assert set(snaps) == {"zz98_dummy", "zz99_boom"}
            assert snaps["zz99_boom"].equity == pytest.approx(100000.0)
            dummy = snaps["zz98_dummy"]
            assert dummy.mode == "paper"
            # equity = capital + day pnl (small slippage loss on the open position)
            assert dummy.equity == pytest.approx(100000.0 + dummy.day_pnl)


# =========================================================================== scheduler
class TestSchedulerWiring:
    def test_build_scheduler_registers_all_jobs(self, monkeypatch):
        from algobot.engine import scheduler as sched_mod

        engine = object.__new__(sched_mod.EngineService)  # skip stack build
        engine.live_enabled = False
        engine.runner = None
        engine.risk = None
        engine.order_manager = None
        sched = sched_mod.build_scheduler(engine)
        try:
            job_ids = {j.id for j in sched.get_jobs()}
        finally:
            sched.shutdown(wait=False) if sched.running else None
        assert job_ids == {"token_refresh", "scan_5min", "scan_15min",
                           "scan_expiry", "scan_0920", "monitor", "squareoff",
                           "eod", "gate", "snapshot", "risk_rollover",
                           "heartbeat"}

    def test_week_month_boundary_helpers(self):
        from algobot.engine import scheduler as sched_mod

        # Fri 2026-06-26 is the last trading day of its week
        assert sched_mod._is_last_trading_day_of_week(dt.date(2026, 6, 26))
        assert not sched_mod._is_last_trading_day_of_week(dt.date(2026, 6, 24))
        # Wed 2026-07-01 is the first trading day of July
        assert sched_mod._is_first_trading_day_of_month(dt.date(2026, 7, 1))
        assert not sched_mod._is_first_trading_day_of_month(dt.date(2026, 7, 2))
