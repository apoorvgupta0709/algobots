"""Broker + execution layer tests: paper fills, risk caps, order manager,
position monitor R-management, squareoff and Fyers adapters.

No network. A throwaway sqlite DATABASE_URL is set BEFORE any algobot import
so all persistence lands in a temp file; tables are wiped per test.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
from types import SimpleNamespace

_TMPDIR = tempfile.mkdtemp(prefix="algobot-exec-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"

import pytest

from algobot.core import config as config_mod

config_mod.settings.cache_clear()

from algobot.persistence import db as db_mod

db_mod.get_engine.cache_clear()
db_mod.get_sessionmaker.cache_clear()

from algobot.broker.base import BrokerInterface
from algobot.broker.fyers.broker import FyersBroker
from algobot.broker.fyers.symbols import is_future, is_option
from algobot.broker.paper import PaperBroker
from algobot.core.clock import now_ist
from algobot.core.enums import (
    ExitReason,
    Mode,
    OptionType,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    SignalType,
)
from algobot.core.exceptions import BrokerError, RiskRejection
from algobot.core.models import (
    ExpiryRule,
    OptionLeg,
    OptionStructure,
    Order,
    Signal,
    StrikeRule,
)
from algobot.costs.india import CostModel
from algobot.execution.order_manager import OrderManager
from algobot.execution.position_monitor import PositionMonitor
from algobot.execution.risk import RiskEngine
from algobot.execution.squareoff import squareoff_intraday
from algobot.persistence.db import session_scope
from algobot.persistence.schema import (
    Base,
    EventLogRow,
    FillRow,
    OrderRow,
    PositionRow,
    RiskStateRow,
    SignalRow,
    TradeRow,
)

COST = CostModel()


@pytest.fixture(autouse=True)
def clean_db():
    db_mod.init_db()
    with session_scope() as s:
        for table in reversed(Base.metadata.sorted_tables):
            s.execute(table.delete())
    yield


# --------------------------------------------------------------------------- helpers
def dict_quote_fn(quotes: dict):
    return lambda symbols: {s: quotes[s] for s in symbols if s in quotes}


def make_signal(signal_type=SignalType.ENTRY_LONG, instrument="NSE:SBIN-EQ",
                reference=100.0, stop=99.0, tp=None, **kw) -> Signal:
    return Signal(strategy_id=kw.pop("strategy_id", "s1"), signal_type=signal_type,
                  instrument=instrument, timestamp=now_ist(),
                  reference_price=reference, stop_loss=stop, take_profit=tp, **kw)


class StubBroker(BrokerInterface):
    """Records orders; does NOT maintain DB rows (live-broker-like)."""

    def __init__(self, quotes: dict | None = None):
        self.orders: list[Order] = []
        self.quotes = quotes or {}

    @property
    def name(self) -> str:
        return "fyers"

    def place_order(self, order: Order) -> Order:
        self.orders.append(order)
        order.broker_order_id = f"S{len(self.orders)}"
        order.status = OrderStatus.PLACED
        return order

    def cancel_order(self, broker_order_id: str) -> bool:
        return True

    def get_quotes(self, symbols):
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    def get_positions(self):
        return []

    def get_funds(self) -> float:
        return 1_000_000.0


# =========================================================================== paper broker
class TestPaperBroker:
    def test_market_round_trip_winner(self):
        quotes = {"NSE:SBIN-EQ": 100.0}
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))

        entry = broker.place_order(Order(strategy_id="s1", symbol="NSE:SBIN-EQ",
                                         side=Side.BUY, qty=10))
        assert entry.status == OrderStatus.FILLED
        assert entry.broker_order_id.startswith("P")
        buy_px = COST.apply_slippage("NSE:SBIN-EQ", Side.BUY, 100.0)

        with session_scope() as s:
            orow = s.query(OrderRow).one()
            assert (orow.mode, orow.status) == ("paper", "filled")
            fill = s.query(FillRow).one()
            assert fill.price == pytest.approx(buy_px)
            pos = s.query(PositionRow).one()
            assert (pos.qty, pos.status) == (10, "open")
            assert pos.avg_price == pytest.approx(buy_px)

        positions = broker.get_positions()
        assert len(positions) == 1 and positions[0].qty == 10

        quotes["NSE:SBIN-EQ"] = 110.0
        broker.place_order(Order(strategy_id="s1", symbol="NSE:SBIN-EQ",
                                 side=Side.SELL, qty=10, tag="s1:tp"))
        sell_px = COST.apply_slippage("NSE:SBIN-EQ", Side.SELL, 110.0)

        with session_scope() as s:
            assert s.query(PositionRow).filter_by(status="open").count() == 0
            trade = s.query(TradeRow).one()
            assert trade.exit_reason == ExitReason.TAKE_PROFIT.value
            assert trade.entry_price == pytest.approx(buy_px)
            assert trade.exit_price == pytest.approx(sell_px)
            assert trade.costs > 0
            assert trade.gross_pnl == pytest.approx((sell_px - buy_px) * 10, abs=0.02)
            assert trade.net_pnl > 0                      # winner stays a winner
            assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.costs, abs=0.02)

    def test_market_round_trip_loser(self):
        quotes = {"NSE:SBIN-EQ": 100.0}
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))
        broker.place_order(Order(strategy_id="s1", symbol="NSE:SBIN-EQ",
                                 side=Side.BUY, qty=10))
        quotes["NSE:SBIN-EQ"] = 90.0
        broker.place_order(Order(strategy_id="s1", symbol="NSE:SBIN-EQ",
                                 side=Side.SELL, qty=10, tag="s1:sl"))
        with session_scope() as s:
            trade = s.query(TradeRow).one()
            assert trade.net_pnl < 0
            assert trade.exit_reason == ExitReason.STOP_LOSS.value

    def test_unrecognized_tag_maps_to_signal(self):
        quotes = {"NSE:SBIN-EQ": 100.0}
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))
        broker.place_order(Order(strategy_id="s1", symbol="NSE:SBIN-EQ",
                                 side=Side.BUY, qty=5))
        broker.place_order(Order(strategy_id="s1", symbol="NSE:SBIN-EQ",
                                 side=Side.SELL, qty=5, tag="whatever"))
        with session_scope() as s:
            assert s.query(TradeRow).one().exit_reason == ExitReason.SIGNAL.value

    def test_no_quote_fn_fallbacks(self):
        broker = PaperBroker()  # no quote source at all

        # 1. falls back to the limit price
        o1 = broker.place_order(Order(strategy_id="s1", symbol="NSE:XYZ-EQ",
                                      side=Side.BUY, qty=10, limit_price=200.0))
        assert o1.status == OrderStatus.FILLED
        first_fill = COST.apply_slippage("NSE:XYZ-EQ", Side.BUY, 200.0)
        with session_scope() as s:
            assert s.query(FillRow).one().price == pytest.approx(first_fill)

        # 2. falls back to the last known position price
        o2 = broker.place_order(Order(strategy_id="s1", symbol="NSE:XYZ-EQ",
                                      side=Side.SELL, qty=10))
        assert o2.status == OrderStatus.FILLED
        with session_scope() as s:
            trade = s.query(TradeRow).one()
            assert trade.exit_price == pytest.approx(
                COST.apply_slippage("NSE:XYZ-EQ", Side.SELL, first_fill))

        # 3. falls back to the stop price
        o3 = broker.place_order(Order(strategy_id="s1", symbol="NSE:ABC-EQ",
                                      side=Side.BUY, qty=1, stop_price=150.0))
        assert o3.status == OrderStatus.FILLED

        # 4. nothing to price against: rejected, never crashes
        o4 = broker.place_order(Order(strategy_id="s1", symbol="NSE:NEW-EQ",
                                      side=Side.BUY, qty=1))
        assert o4.status == OrderStatus.REJECTED

    def test_limit_not_marketable_stays_placed(self):
        quotes = {"NSE:SBIN-EQ": 100.0}
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))
        order = broker.place_order(Order(strategy_id="s1", symbol="NSE:SBIN-EQ",
                                         side=Side.BUY, qty=10,
                                         order_type=OrderType.LIMIT, limit_price=95.0))
        assert order.status == OrderStatus.PLACED
        assert broker.cancel_order(order.broker_order_id) is True

    def test_get_funds_subtracts_open_notional(self):
        quotes = {"NSE:SBIN-EQ": 100.0}
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))
        capital = float(config_mod.settings()["capital"])
        assert broker.get_funds() == pytest.approx(capital)
        broker.place_order(Order(strategy_id="s1", symbol="NSE:SBIN-EQ",
                                 side=Side.BUY, qty=10))
        buy_px = COST.apply_slippage("NSE:SBIN-EQ", Side.BUY, 100.0)
        assert broker.get_funds() == pytest.approx(capital - 10 * buy_px)


# =========================================================================== risk engine
class TestRiskEngine:
    def test_size_qty_math(self):
        risk = RiskEngine(capital=100_000)
        assert risk.size_qty(100.0, 99.0, 100_000) == 750       # 0.75% of 1L / Re 1
        assert risk.size_qty(100.0, 99.0, 100_000, lot=65) == 715  # 11 lots x 65
        assert risk.size_qty(100.0, 100.0, 100_000) == 0        # degenerate stop
        assert risk.size_qty(100.0, 99.0, 100_000, lot=1000) == 0  # lot unaffordable

    def test_kill_switch_blocks_and_persists(self):
        risk = RiskEngine(capital=100_000)
        risk.set_kill_switch(True, "manual halt")
        with pytest.raises(RiskRejection, match="kill switch"):
            risk.check(make_signal(), Mode.PAPER, 0, risk.day_state())
        # EXIT signals always pass
        risk.check(make_signal(SignalType.EXIT), Mode.PAPER, 0, risk.day_state())
        # persists across engine instances + journalled
        assert RiskEngine(capital=100_000).is_killed() is True
        with session_scope() as s:
            assert s.query(EventLogRow).filter_by(source="risk").count() == 1
        risk.set_kill_switch(False, "resume")
        assert risk.is_killed() is False

    def test_daily_loss_cap(self):
        risk = RiskEngine(capital=100_000)  # cap 2.5% = 2500
        risk.update_day_state(pnl_delta=-2600.0)
        with pytest.raises(RiskRejection, match="daily loss cap"):
            risk.check(make_signal(), Mode.PAPER, 0, risk.day_state())

    def test_weekly_loss_cap_recomputed_from_trades(self):
        risk = RiskEngine(capital=100_000)  # cap 5% = 5000
        now = now_ist().replace(tzinfo=None)
        with session_scope() as s:
            s.add(TradeRow(strategy_id="s1", mode="paper", symbol="NSE:SBIN-EQ",
                           direction="long", qty=10, entry_time=now, exit_time=now,
                           entry_price=100, exit_price=40, gross_pnl=-6000,
                           costs=0, net_pnl=-6000, exit_reason="sl"))
        state = risk.day_state()
        assert state.realized_week_pnl == pytest.approx(-6000)
        with pytest.raises(RiskRejection, match="weekly loss cap"):
            risk.check(make_signal(), Mode.PAPER, 0, state)

    def test_max_concurrent_positions(self):
        risk = RiskEngine(capital=100_000)  # max 3
        with pytest.raises(RiskRejection, match="max concurrent positions"):
            risk.check(make_signal(), Mode.PAPER, 3, risk.day_state())

    def test_global_max_trades_per_day(self):
        risk = RiskEngine(capital=100_000)  # max 10
        risk.update_day_state(trades_delta=10)
        with pytest.raises(RiskRejection, match="max trades per day"):
            risk.check(make_signal(), Mode.PAPER, 0, risk.day_state())


# =========================================================================== order manager
class TestOrderManager:
    def _om(self, broker, mode=Mode.LIVE):
        risk = RiskEngine(capital=100_000)
        return OrderManager({mode: broker}, risk, CostModel()), risk

    def test_entry_sizing_and_stamping_with_stub_broker(self):
        stub = StubBroker(quotes={"NSE:SBIN-EQ": 100.0})
        om, risk = self._om(stub)
        signal = make_signal(reference=100.0, stop=99.0, tp=106.0)

        orders = om.submit(signal, Mode.LIVE, capital=100_000)
        assert len(orders) == 1
        assert (orders[0].side, orders[0].qty) == (Side.BUY, 750)
        assert stub.orders[0] is orders[0]

        with session_scope() as s:
            sig = s.query(SignalRow).one()
            assert sig.status == "executed"
            pos = s.query(PositionRow).one()  # created by OM: stub keeps no DB book
            assert (pos.mode, pos.qty, pos.status) == ("live", 750, "open")
            assert pos.avg_price == pytest.approx(100.0)
            assert (pos.stop_loss, pos.take_profit) == (99.0, 106.0)
            assert pos.underlying == "NSE:SBIN-EQ"
            assert pos.underlying_entry == pytest.approx(100.0)
        assert risk.day_state().trades_today == 1

    def test_exit_signal_reverse_side_and_trade_row(self):
        stub = StubBroker(quotes={"NSE:SBIN-EQ": 100.0})
        om, risk = self._om(stub)
        om.submit(make_signal(reference=100.0, stop=99.0), Mode.LIVE, 100_000)

        stub.quotes["NSE:SBIN-EQ"] = 105.0
        exits = om.submit(make_signal(SignalType.EXIT, reference=105.0, stop=None),
                          Mode.LIVE, 100_000)
        assert len(exits) == 1
        assert (exits[0].side, exits[0].qty) == (Side.SELL, 750)

        with session_scope() as s:
            assert s.query(PositionRow).filter_by(status="open").count() == 0
            trade = s.query(TradeRow).one()
            assert trade.exit_reason == ExitReason.SIGNAL.value
            assert trade.modeled_exit_price == pytest.approx(105.0)
            assert trade.exit_price == pytest.approx(105.0)
            assert trade.gross_pnl == pytest.approx(5.0 * 750)
            assert trade.costs > 0 and trade.net_pnl < trade.gross_pnl
            net = trade.net_pnl
        assert risk.day_state().realized_day_pnl == pytest.approx(net)

    def test_risk_rejection_marks_signal(self):
        stub = StubBroker(quotes={"NSE:SBIN-EQ": 100.0})
        om, risk = self._om(stub)
        risk.set_kill_switch(True, "halt")
        orders = om.submit(make_signal(), Mode.LIVE, 100_000)
        assert orders == [] and stub.orders == []
        with session_scope() as s:
            sig = s.query(SignalRow).one()
            assert sig.status == "rejected" and "kill" in sig.reject_reason

    def test_structure_legs_share_structure_id(self):
        class StubLegBuilder:
            def resolve(self, structure, spot, now):
                for i, leg in enumerate(structure.legs):
                    leg.resolved_symbol = (f"NSE:NIFTY26JUL{24500 + i * 100}"
                                           f"{leg.option_type.value}")
                return structure

        stub = StubBroker(quotes={})
        risk = RiskEngine(capital=500_000)
        om = OrderManager({Mode.LIVE: stub}, risk, CostModel(),
                          leg_builder=StubLegBuilder())
        structure = OptionStructure(
            name="short_straddle", underlying="NSE:NIFTY50-INDEX",
            legs=[OptionLeg(Side.SELL, OptionType.CE, StrikeRule.atm(),
                            ExpiryRule.weekly()),
                  OptionLeg(Side.SELL, OptionType.PE, StrikeRule.atm(),
                            ExpiryRule.weekly())],
            net_direction="credit")
        signal = make_signal(instrument="NSE:NIFTY50-INDEX", reference=24500.0,
                             stop=24650.0, strategy_id="op1", structure=structure)

        orders = om.submit(signal, Mode.LIVE, 500_000)
        assert len(orders) == 2
        assert all(o.qty == 65 for o in orders)          # 1 lot x NIFTY lot size
        assert all(o.side == Side.SELL for o in orders)
        assert orders[0].tag == orders[1].tag            # shared structure id tag
        with session_scope() as s:
            rows = s.query(PositionRow).all()
            assert len(rows) == 2
            assert rows[0].structure_id and rows[0].structure_id == rows[1].structure_id
            assert all(r.underlying == "NSE:NIFTY50-INDEX" for r in rows)

    def test_structure_sized_to_zero_when_margin_exceeds_capital(self):
        class StubLegBuilder:
            def resolve(self, structure, spot, now):
                for i, leg in enumerate(structure.legs):
                    leg.resolved_symbol = (f"NSE:NIFTY26JUL{24500 + i * 100}"
                                           f"{leg.option_type.value}")
                return structure

        stub = StubBroker(quotes={})
        # Tiny capital so one lot's SPAN margin exceeds it -> no orders.
        risk = RiskEngine(capital=10_000)
        om = OrderManager({Mode.LIVE: stub}, risk, CostModel(),
                          leg_builder=StubLegBuilder())
        structure = OptionStructure(
            name="short_straddle", underlying="NSE:NIFTY50-INDEX",
            legs=[OptionLeg(Side.SELL, OptionType.CE, StrikeRule.atm(),
                            ExpiryRule.weekly()),
                  OptionLeg(Side.SELL, OptionType.PE, StrikeRule.atm(),
                            ExpiryRule.weekly())],
            net_direction="credit")
        signal = make_signal(instrument="NSE:NIFTY50-INDEX", reference=24500.0,
                             stop=24650.0, strategy_id="op1", structure=structure)

        orders = om.submit(signal, Mode.LIVE, 10_000)
        assert orders == [] and stub.orders == []
        with session_scope() as s:
            sig = s.query(SignalRow).one()
            assert sig.status == "rejected"

    def test_flatten_closes_book_and_rolls_day_state(self):
        quotes = {"NSE:AAA-EQ": 100.0, "NSE:BBB-EQ": 50.0}
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))
        risk = RiskEngine(capital=100_000)
        om = OrderManager({Mode.PAPER: broker}, risk, CostModel())
        om.submit(make_signal(instrument="NSE:AAA-EQ", reference=100, stop=99,
                              strategy_id="s1"), Mode.PAPER, 100_000)
        om.submit(make_signal(instrument="NSE:BBB-EQ", reference=50, stop=49.5,
                              strategy_id="s2"), Mode.PAPER, 100_000)
        with session_scope() as s:
            assert s.query(PositionRow).filter_by(status="open").count() == 2

        orders = om.flatten(None, Mode.PAPER, ExitReason.KILL)
        assert len(orders) == 2
        with session_scope() as s:
            assert s.query(PositionRow).filter_by(status="open").count() == 0
            trades = s.query(TradeRow).all()
            assert len(trades) == 2
            assert all(t.exit_reason == ExitReason.KILL.value for t in trades)
            total_net = sum(t.net_pnl for t in trades)
        state = risk.day_state()
        assert state.realized_day_pnl == pytest.approx(total_net)
        assert state.open_position_count == 0


# =========================================================================== monitor
class TestPositionMonitor:
    SYM = "NSE:NIFTY2670724500CE"
    UND = "NSE:NIFTY50-INDEX"

    def _setup(self, quotes, stop=24400.0, tp=None, entry=24500.0, qty=65,
               avg=50.0):
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))
        risk = RiskEngine(capital=500_000)
        om = OrderManager({Mode.PAPER: broker}, risk, CostModel())
        monitor = PositionMonitor(om, dict_quote_fn(quotes), risk)
        with session_scope() as s:
            s.add(PositionRow(strategy_id="opt1", mode="paper", symbol=self.SYM,
                              qty=qty, avg_price=avg, stop_loss=stop, take_profit=tp,
                              underlying=self.UND, underlying_entry=entry,
                              product_type="INTRADAY",
                              opened_at=now_ist().replace(tzinfo=None)))
        return monitor

    def _pos(self):
        with session_scope() as s:
            return s.query(PositionRow).order_by(PositionRow.id).first()

    def test_ratchet_up_never_loosen_then_trail_fire(self):
        # long-underlying view: entry 24500, stop 24400 (R = 100)
        quotes = {self.UND: 24590.0, self.SYM: 95.0}
        monitor = self._setup(quotes)

        # +0.9R favourable: breakeven then ratchet locks 60% of +90 = 24554
        monitor.tick()
        pos = self._pos()
        assert pos.status == "open"
        assert pos.trail_anchor == pytest.approx(24590.0)
        assert pos.stop_loss == pytest.approx(24554.0)
        assert pos.last_price == pytest.approx(95.0)
        assert pos.unrealized_pnl == pytest.approx((95.0 - 50.0) * 65)

        # pullback that stays above the stop: anchor and stop never loosen
        quotes[self.UND] = 24560.0
        monitor.tick()
        pos = self._pos()
        assert pos.status == "open"
        assert pos.trail_anchor == pytest.approx(24590.0)
        assert pos.stop_loss == pytest.approx(24554.0)

        # drop through the ratcheted stop: trail exit at modeled level
        quotes[self.UND] = 24550.0
        quotes[self.SYM] = 80.0
        monitor.tick()
        pos = self._pos()
        assert pos.status == "closed"
        with session_scope() as s:
            trade = s.query(TradeRow).one()
            assert trade.exit_reason == ExitReason.TRAIL.value
            assert trade.modeled_exit_price == pytest.approx(24554.0)
            assert trade.exit_price == pytest.approx(
                COST.apply_slippage(self.SYM, Side.SELL, 80.0))

    def test_plain_stop_loss_is_not_trail(self):
        quotes = {self.UND: 24390.0, self.SYM: 20.0}
        monitor = self._setup(quotes)  # stop 24400 never ratcheted
        monitor.tick()
        with session_scope() as s:
            trade = s.query(TradeRow).one()
            assert trade.exit_reason == ExitReason.STOP_LOSS.value
            assert trade.modeled_exit_price == pytest.approx(24400.0)

    def test_short_side_take_profit_mirror(self):
        # bearish view expressed with long puts: stop ABOVE entry, TP below
        quotes = {self.UND: 24290.0, "NSE:NIFTY2670724500PE": 210.0}
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))
        risk = RiskEngine(capital=500_000)
        om = OrderManager({Mode.PAPER: broker}, risk, CostModel())
        monitor = PositionMonitor(om, dict_quote_fn(quotes), risk)
        with session_scope() as s:
            s.add(PositionRow(strategy_id="opt2", mode="paper",
                              symbol="NSE:NIFTY2670724500PE", qty=65, avg_price=60.0,
                              stop_loss=24600.0, take_profit=24300.0,
                              underlying=self.UND, underlying_entry=24500.0,
                              product_type="INTRADAY",
                              opened_at=now_ist().replace(tzinfo=None)))
        monitor.tick()
        with session_scope() as s:
            pos = s.query(PositionRow).one()
            assert pos.status == "closed"
            trade = s.query(TradeRow).one()
            assert trade.exit_reason == ExitReason.TAKE_PROFIT.value
            assert trade.modeled_exit_price == pytest.approx(24300.0)

    def test_on_update_callback_invoked(self):
        quotes = {self.UND: 24510.0, self.SYM: 55.0}
        seen = []
        monitor = self._setup(quotes)
        monitor.on_update = lambda pos, q: seen.append((pos.symbol, q[self.SYM]))
        monitor.tick()
        assert seen == [(self.SYM, 55.0)]


# =========================================================================== squareoff
class TestSquareoff:
    def test_squareoff_after_1515(self):
        quotes = {"NSE:AAA-EQ": 100.0, "NSE:BBB-EQ": 50.0}
        broker = PaperBroker(quote_fn=dict_quote_fn(quotes))
        risk = RiskEngine(capital=100_000)
        om = OrderManager({Mode.PAPER: broker}, risk, CostModel())
        broker.place_order(Order(strategy_id="id1", symbol="NSE:AAA-EQ",
                                 side=Side.BUY, qty=10,
                                 product_type=ProductType.INTRADAY))
        broker.place_order(Order(strategy_id="lt1", symbol="NSE:BBB-EQ",
                                 side=Side.BUY, qty=10,
                                 product_type=ProductType.CNC))

        early = dt.datetime(2026, 7, 3, 15, 0)
        assert squareoff_intraday(om, now=early) == []
        with session_scope() as s:
            assert s.query(PositionRow).filter_by(status="open").count() == 2

        late = dt.datetime(2026, 7, 3, 15, 16)
        orders = squareoff_intraday(om, now=late)
        assert len(orders) == 1                     # only the INTRADAY product
        with session_scope() as s:
            open_rows = s.query(PositionRow).filter_by(status="open").all()
            assert [r.symbol for r in open_rows] == ["NSE:BBB-EQ"]
            trade = s.query(TradeRow).one()
            assert trade.exit_reason == ExitReason.SQUAREOFF.value

        # strategy meta can request squareoff for non-INTRADAY products
        metas = {"lt1": SimpleNamespace(intraday_squareoff=True)}
        orders = squareoff_intraday(om, registry_metas=metas, now=late)
        assert len(orders) == 1
        with session_scope() as s:
            assert s.query(PositionRow).filter_by(status="open").count() == 0


# =========================================================================== fyers symbols
class TestFyersSymbols:
    def test_monthly_option(self):
        assert is_option("NSE:NIFTY26JUL24500CE")
        assert is_option("NSE:BANKNIFTY26AUG52000PE")
        assert not is_future("NSE:NIFTY26JUL24500CE")

    def test_weekly_option_month_codes(self):
        assert is_option("NSE:NIFTY2670724500CE")       # July (7)
        assert is_option("NSE:NIFTY26O0724500CE")       # October (O)
        assert is_option("NSE:BANKNIFTY26N1152000PE")   # November (N)
        assert is_option("NSE:NIFTY26D3024500PE")       # December (D)

    def test_futures(self):
        assert is_future("NSE:NIFTY26JULFUT")
        assert is_future("NSE:M&M26JANFUT")
        assert not is_option("NSE:NIFTY26JULFUT")

    def test_cash_short_circuit(self):
        for sym in ("NSE:SBIN-EQ", "NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"):
            assert not is_option(sym)
            assert not is_future(sym)


# =========================================================================== fyers broker
class FakeFyersClient:
    def __init__(self):
        self.last_payload = None
        self.place_resp = {"s": "ok", "id": "24070300001"}

    def place_order(self, data):
        self.last_payload = data
        return self.place_resp

    def cancel_order(self, data):
        self.last_cancel = data
        return {"s": "ok"}

    def quotes(self, data):
        self.last_quotes_req = data
        return {"s": "ok", "d": [{"n": "NSE:SBIN-EQ", "v": {"lp": 505.5}},
                                 {"n": "NSE:TCS-EQ", "v": {"lp": 3900.0}}]}

    def positions(self):
        return {"s": "ok", "netPositions": [
            {"symbol": "NSE:SBIN-EQ", "netQty": 10, "netAvg": 500.0,
             "productType": "INTRADAY"},
            {"symbol": "NSE:TCS-EQ", "netQty": 0, "netAvg": 3900.0,
             "productType": "INTRADAY"},
        ]}

    def funds(self):
        return {"s": "ok", "fund_limit": [
            {"id": 1, "title": "Total Balance", "equityAmount": 999.0},
            {"id": 10, "title": "Available Balance", "equityAmount": 123456.78},
        ]}


class TestFyersBroker:
    def test_place_order_payload_and_response(self, monkeypatch):
        monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", "true")  # open the fuse
        fake = FakeFyersClient()
        broker = FyersBroker(client=fake)
        assert broker.name == "fyers"
        order = Order(strategy_id="s1", symbol="NSE:NIFTY26JUL24500CE",
                      side=Side.SELL, qty=65, order_type=OrderType.LIMIT,
                      limit_price=101.5, product_type=ProductType.MARGIN,
                      tag="op1:abc")
        out = broker.place_order(order)
        assert fake.last_payload == {
            "symbol": "NSE:NIFTY26JUL24500CE", "qty": 65, "type": 1, "side": -1,
            "productType": "MARGIN", "limitPrice": 101.5, "stopPrice": 0.0,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
            "orderTag": "op1:abc",
        }
        assert out.broker_order_id == "24070300001"
        assert out.status == OrderStatus.PLACED

    def test_place_order_error_raises_broker_error(self, monkeypatch):
        monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", "true")  # open the fuse
        fake = FakeFyersClient()
        fake.place_resp = {"s": "error", "message": "RMS: margin shortfall"}
        broker = FyersBroker(client=fake)
        order = Order(strategy_id="s1", symbol="NSE:SBIN-EQ", side=Side.BUY, qty=1)
        with pytest.raises(BrokerError, match="margin shortfall"):
            broker.place_order(order)
        assert order.status == OrderStatus.REJECTED

    def test_quotes_positions_funds(self):
        broker = FyersBroker(client=FakeFyersClient())
        quotes = broker.get_quotes(["NSE:SBIN-EQ", "NSE:TCS-EQ"])
        assert quotes == {"NSE:SBIN-EQ": 505.5, "NSE:TCS-EQ": 3900.0}
        assert broker._client.last_quotes_req == {"symbols": "NSE:SBIN-EQ,NSE:TCS-EQ"}

        positions = broker.get_positions()
        assert len(positions) == 1                      # zero-qty row skipped
        assert positions[0].strategy_id == "external"
        assert (positions[0].symbol, positions[0].qty) == ("NSE:SBIN-EQ", 10)
        assert positions[0].avg_price == pytest.approx(500.0)
        assert positions[0].mode == Mode.LIVE

        assert broker.get_funds() == pytest.approx(123456.78)

    def test_cancel_order(self):
        broker = FyersBroker(client=FakeFyersClient())
        assert broker.cancel_order("X1") is True
        assert broker._client.last_cancel == {"id": "X1"}
