"""Backtest engine tests: risk sizing, R-management, schedules, options."""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from algobot.backtest.compat import ensure_strategy_deps

ensure_strategy_deps()  # no-op when the sibling subsystems are present

from algobot.backtest.engine import BacktestEngine  # noqa: E402
from algobot.core.enums import (Category, ExitReason, OptionType, Side,  # noqa: E402
                                SignalType, Timeframe)
from algobot.core.models import (ExpiryRule, OptionLeg, OptionStructure,  # noqa: E402
                                 Signal, SizeHint, StrikeRule)
from algobot.core.strategy import (SCAN_EOD, SCAN_EVERY_5MIN, SCAN_MONTHLY,  # noqa: E402
                                   StrategyBase, StrategyMeta)
from tests.fixtures.synthetic import equity_daily, index_5min  # noqa: E402

IST = "Asia/Kolkata"
SYM = "NSE:TEST-EQ"
IDX = "NSE:NIFTY50-INDEX"


# --------------------------------------------------------------------- helpers
def make_meta(**overrides) -> StrategyMeta:
    base = dict(strategy_id="t_test", name="test", category=Category.INTRADAY,
                timeframe=Timeframe.MIN5, scan_schedule=SCAN_EVERY_5MIN,
                instruments=[SYM], warmup_bars=1, max_positions=1,
                max_trades_per_day=5, intraday_squareoff=False)
    base.update(overrides)
    return StrategyMeta(**base)


def flat_day(day: str = "2026-06-01", price: float = 100.0) -> pd.DataFrame:
    """One flat NSE session of 5-min bars at ``price`` (o=h=l=c)."""
    idx = pd.date_range(f"{day} 09:15", f"{day} 15:25", freq="5min", tz=IST)
    df = pd.DataFrame({"open": price, "high": price, "low": price,
                       "close": price, "volume": 0}, index=idx, dtype=float)
    return df


def set_bar(df: pd.DataFrame, ts: str, o: float, h: float, l: float, c: float) -> None:
    df.loc[pd.Timestamp(ts, tz=IST), ["open", "high", "low", "close"]] = [o, h, l, c]


class EnterAt(StrategyBase):
    """Enter long once per day at a fixed bar time with % stop/target."""
    meta = make_meta()

    def __init__(self, at="10:00", stop_pct=0.5, target_pct=None,
                 stop_abs=None, target_abs=None, params=None):
        super().__init__(params)
        self.at = dt.time.fromisoformat(at)
        self.stop_pct, self.target_pct = stop_pct, target_pct
        self.stop_abs, self.target_abs = stop_abs, target_abs

    def generate_signals(self, data, ctx):
        if ctx.has_open_position or ctx.now.time() != self.at:
            return []
        sym, df = next(iter(data.items()))
        c = float(df.close.iloc[-1])
        stop = self.stop_abs if self.stop_abs is not None else \
            (c * (1 - self.stop_pct / 100) if self.stop_pct else None)
        target = self.target_abs if self.target_abs is not None else \
            (c * (1 + self.target_pct / 100) if self.target_pct else None)
        return [Signal(strategy_id=self.strategy_id,
                       signal_type=SignalType.ENTRY_LONG, instrument=sym,
                       timestamp=ctx.now, reference_price=c,
                       stop_loss=stop, take_profit=target)]


# --------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def fixture_run():
    """One engine run of an always-in intraday strategy on fixture data."""
    class Strat(EnterAt):
        meta = make_meta(intraday_squareoff=True)
    df = index_5min(days=8, seed=11, start_price=800)
    engine = BacktestEngine(Strat(at="10:00", stop_pct=0.5, target_pct=1.0),
                            {SYM: df}, capital=100_000)
    return df, engine.run()


# --------------------------------------------------------------------- tests
def test_engine_runs_with_trades_and_costs(fixture_run):
    df, res = fixture_run
    assert len(res.trades) > 0
    assert all(t.costs > 0 for t in res.trades)
    assert res.metrics["total_costs"] > 0
    # per-bar mark-to-market: equity has exactly one point per bar
    assert len(res.equity) == len(df)
    assert list(res.equity.index) == list(df.index)
    assert res.equity.notna().all()


def test_metrics_sane(fixture_run):
    _df, res = fixture_run
    m = res.metrics
    for key in ("net_pnl", "gross_pnl", "total_costs", "n_trades", "win_rate",
                "profit_factor", "expectancy_r", "avg_win", "avg_loss",
                "max_drawdown_pct", "sharpe", "cagr_pct", "exposure"):
        assert key in m, key
    assert m["n_trades"] == len(res.trades)
    assert 0.0 <= m["win_rate"] <= 1.0
    assert 0.0 <= m["exposure"] <= 1.0
    assert m["max_drawdown_pct"] >= 0.0
    assert abs(m["net_pnl"] - (m["gross_pnl"] - m["total_costs"])) < 1.0


def test_stop_loss_exit_at_or_below_stop():
    df = flat_day(price=100.0)
    set_bar(df, "2026-06-01 10:00", 100.0, 100.0, 99.0, 99.2)  # crashes through
    strat = EnterAt(at="09:20", stop_abs=99.5)                 # fill 09:25 @100
    res = BacktestEngine(strat, {SYM: df}, capital=100_000).run()
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == ExitReason.STOP_LOSS
    assert t.modeled_exit_price == pytest.approx(99.5)
    assert t.exit_price <= 99.5                 # sell-side slippage: at/below stop
    assert t.exit_time.time() == dt.time(10, 0)


def test_breakeven_and_ratchet_trail():
    # entry 100, stop 99 (R=1); rallies to 103 (=3R -> BE + ratchet locks 60%
    # of MFE at 101.8); pullback tags the ratcheted stop -> TRAIL exit.
    df = flat_day(price=100.0)
    set_bar(df, "2026-06-01 09:30", 100.0, 103.0, 100.0, 102.5)
    set_bar(df, "2026-06-01 09:35", 102.4, 102.5, 100.9, 101.0)
    strat = EnterAt(at="09:20", stop_abs=99.0)
    res = BacktestEngine(strat, {SYM: df}, capital=100_000).run()
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == ExitReason.TRAIL
    assert t.modeled_exit_price == pytest.approx(100.0 + 0.6 * 3.0)  # 101.8
    assert t.net_pnl > 0                        # winner scratched above entry
    assert t.exit_time.time() == dt.time(9, 35)


def test_no_scratch_before_breakeven_threshold():
    # +0.5R favourable move only: ratchet must NOT engage (else every trade
    # scratches at the first tick of profit); trade squares off at 15:15.
    df = flat_day(price=100.0)
    set_bar(df, "2026-06-01 09:30", 100.0, 100.5, 100.0, 100.2)
    set_bar(df, "2026-06-01 09:35", 100.2, 100.2, 100.05, 100.1)

    class Strat(EnterAt):
        meta = make_meta(intraday_squareoff=True)
    res = BacktestEngine(Strat(at="09:20", stop_abs=99.0),
                         {SYM: df}, capital=100_000).run()
    assert len(res.trades) == 1
    assert res.trades[0].exit_reason == ExitReason.SQUAREOFF


def test_intraday_squareoff_flattens_by_1520():
    class Strat(EnterAt):
        meta = make_meta(intraday_squareoff=True)
    df = flat_day(price=100.0)
    res = BacktestEngine(Strat(at="14:30", stop_abs=90.0),
                         {SYM: df}, capital=100_000).run()
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == ExitReason.SQUAREOFF
    assert t.exit_time.time() >= dt.time(15, 15)
    assert t.exit_time.time() <= dt.time(15, 20)
    assert not res.open_positions               # flat after square-off


def test_option_structure_runs_with_synthetic_provider():
    class LongCall(StrategyBase):
        meta = make_meta(strategy_id="t_call", category=Category.OPTIONS,
                         timeframe=Timeframe.DAY, scan_schedule=SCAN_EOD,
                         instruments=[IDX], warmup_bars=25, max_trades_per_day=1,
                         is_multi_leg=True)

        def generate_signals(self, data, ctx):
            if ctx.has_open_position:
                return []
            df = data[IDX]
            c = float(df.close.iloc[-1])
            structure = OptionStructure(
                name="long_call", underlying=IDX,
                legs=[OptionLeg(Side.BUY, OptionType.CE, StrikeRule.atm(0),
                                ExpiryRule.weekly())],
                net_direction="debit")
            return [Signal(strategy_id=self.strategy_id,
                           signal_type=SignalType.ENTRY_LONG, instrument=IDX,
                           timestamp=ctx.now, reference_price=c,
                           stop_loss=c * 0.98, take_profit=c * 1.02,
                           structure=structure)]

    df = equity_daily(days=60, seed=5, start_price=24000)
    res = BacktestEngine(LongCall(), {IDX: df}, capital=500_000).run()
    assert res.data_source == "synthetic"
    assert len(res.trades) >= 1
    for t in res.trades:
        assert t.symbol == f"long_call:{IDX}"   # structure aggregation
        assert t.structure_json is not None
        assert t.costs > 0
        assert t.direction == "long"            # debit structure
        assert t.qty % 65 == 0                  # NIFTY lot multiples


def test_rebalance_accumulates_without_round_trips():
    class SIP(StrategyBase):
        # max_positions=1 must NOT cap accumulation: REBALANCE is exempt
        meta = make_meta(strategy_id="t_sip", category=Category.LONGTERM,
                         timeframe=Timeframe.DAY, scan_schedule=SCAN_MONTHLY,
                         warmup_bars=1, max_positions=1, max_trades_per_day=1)

        def generate_signals(self, data, ctx):
            sym, df = next(iter(data.items()))
            c = float(df.close.iloc[-1])
            return [Signal(strategy_id=self.strategy_id,
                           signal_type=SignalType.REBALANCE, instrument=sym,
                           timestamp=ctx.now, reference_price=c,
                           size_hint=SizeHint(notional=5000.0))]

    df = equity_daily(days=110, seed=9, start_price=800)
    res = BacktestEngine(SIP(), {SYM: df}, capital=100_000).run()
    assert res.trades == []                     # accumulation: no round trips
    assert len(res.open_positions) > 1          # beyond max_positions=1
    assert all(p.qty > 0 for p in res.open_positions)
    total_qty = sum(p.qty for p in res.open_positions)
    assert 1 <= total_qty <= 100_000 // 700     # ~5000/month at ~800/share


def test_derivative_lot_multiple_sizing():
    class FutTrend(StrategyBase):
        meta = make_meta(strategy_id="t_fut", category=Category.FUTURES,
                         timeframe=Timeframe.DAY, scan_schedule=SCAN_EOD,
                         instruments=[IDX], warmup_bars=2, max_trades_per_day=1)

        def generate_signals(self, data, ctx):
            if ctx.has_open_position:
                return []
            df = data[IDX]
            c = float(df.close.iloc[-1])
            structure = OptionStructure(
                name="index_future", underlying=IDX,
                legs=[OptionLeg(Side.BUY, OptionType.FUT,
                                StrikeRule.absolute(0), ExpiryRule.monthly())],
                net_direction="debit")
            return [Signal(strategy_id=self.strategy_id,
                           signal_type=SignalType.ENTRY_LONG, instrument=IDX,
                           timestamp=ctx.now, reference_price=c,
                           stop_loss=c * 0.99, structure=structure)]

    df = equity_daily(days=15, seed=3, start_price=24000)
    # risk = 0.75% of 50L = 37500; stop ~1% of 24000 => ~156 qty => 2 lots
    res = BacktestEngine(FutTrend(), {IDX: df}, capital=5_000_000).run()
    qtys = [t.qty for t in res.trades] + [abs(p.qty) for p in res.open_positions]
    assert qtys, "expected at least one futures fill"
    for q in qtys:
        assert q % 65 == 0                      # whole NIFTY lots only
    assert max(qtys) >= 2 * 65                  # risk rule sized multiple lots


def test_eod_schedule_holds_overnight():
    class Swing(EnterAt):
        meta = make_meta(strategy_id="t_swing", category=Category.SWING,
                         timeframe=Timeframe.DAY, scan_schedule=SCAN_EOD,
                         warmup_bars=2, intraday_squareoff=False)

        def generate_signals(self, data, ctx):
            if ctx.has_open_position:
                return []
            sym, df = next(iter(data.items()))
            c = float(df.close.iloc[-1])
            return [Signal(strategy_id=self.strategy_id,
                           signal_type=SignalType.ENTRY_LONG, instrument=sym,
                           timestamp=ctx.now, reference_price=c,
                           stop_loss=c * 0.5)]     # far stop: never hit

    df = flat_daily = pd.DataFrame(
        {"open": 800.0, "high": 801.0, "low": 799.0, "close": 800.0,
         "volume": 1000},
        index=pd.DatetimeIndex(
            [pd.Timestamp(d.year, d.month, d.day, 15, 30).tz_localize(IST)
             for d in pd.bdate_range(end="2026-06-30", periods=10)]))
    res = BacktestEngine(Swing(), {SYM: flat_daily}, capital=100_000).run()
    assert not res.trades                       # nothing forced the exit
    assert len(res.open_positions) == 1
    held_days = (df.index[-1].date() - res.open_positions[0].opened_at.date()).days
    assert held_days >= 1                       # carried overnight


def test_cash_index_proxy_not_lot_rounded():
    """CASH instruments size purely by the risk rule even when the symbol
    contains an index root (lot-substring matching would 65x the risk)."""
    class IdxCash(EnterAt):
        meta = make_meta(instruments=[IDX])
    df = flat_day(price=24000.0)
    set_bar(df, "2026-06-01 11:00", 24000, 24000, 23000, 23100)  # hit the stop
    strat = IdxCash(at="09:20", stop_abs=23800.0)                # dist 200
    res = BacktestEngine(strat, {IDX: df}, capital=1_000_000).run()
    assert len(res.trades) == 1
    # risk 7500 / 200 = 37 shares (not 37//65*65 = 0, not 37*65)
    assert res.trades[0].qty == 37


@pytest.mark.parametrize("strategy_id,days", [
    ("id01_orb", 12),
    ("sw04_supertrend_adx", 90),
    ("lt02_sip", 90),
    ("op06_iron_condor", 60),
    ("fu01_trend_positional", 90),
])
def test_reference_strategies_smoke(strategy_id, days):
    """Every reference strategy must run end-to-end without exceptions."""
    from algobot.core.registry import get_strategy
    from scripts.run_backtest import load_data

    cls = get_strategy(strategy_id)
    data, _source = load_data(cls.meta, days)
    res = BacktestEngine(cls(), data, capital=500_000).run()
    assert len(res.equity) > 0
    assert isinstance(res.metrics, dict)
    assert res.data_source in ("real", "synthetic", "mixed")
    for t in res.trades:
        assert t.costs >= 0 and t.exit_time >= t.entry_time
