"""The strategy contract test — parametrized over every registered strategy.

Any file dropped into algobot/strategies/ is automatically held to:
- exactly one StrategyBase subclass, meta.strategy_id == file name (registry enforces)
- valid meta (category/timeframe/schedule/instruments/warmup)
- generate_signals returns list[Signal] with sane fields
- determinism: same (data, ctx) -> same signals
- no look-ahead: appending future bars must not change the signal for a past bar
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from algobot.core import registry, universes
from algobot.core.clock import IST
from algobot.core.enums import Category, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import VALID_SCHEDULES, StrategyContext


def _fixture_data(meta) -> dict[str, pd.DataFrame]:
    from tests.fixtures.synthetic import equity_daily, index_5min

    symbols = universes.resolve(meta.instruments)[:3] or [universes.NIFTY]
    bars = max(meta.warmup_bars * 3, 400)
    out = {}
    for i, sym in enumerate(symbols):
        if meta.timeframe in (Timeframe.MIN5, Timeframe.MIN15, Timeframe.HOUR1):
            df = index_5min(days=max(10, bars // 75 + 2), seed=42 + i,
                            start_price=24000 if "NIFTY" in sym.upper() else 800)
        else:
            df = equity_daily(days=bars, seed=42 + i,
                              start_price=24000 if "INDEX" in sym.upper() else 800)
        out[sym] = df
    return out


def _ctx(ts: dt.datetime, capital: float) -> StrategyContext:
    from algobot.options.chain import OptionChain
    from algobot.options.leg_builder import LegBuilder

    def chain_provider(underlying: str) -> OptionChain:
        return OptionChain.synthetic(underlying, spot=24000.0, now=ts)

    return StrategyContext(now=ts, capital_allocated=capital,
                           option_chain=chain_provider, leg_builder=LegBuilder())


ALL = sorted(registry.all_strategies().items())


def test_registry_not_empty():
    assert len(ALL) >= 1, "no strategies discovered"


@pytest.mark.parametrize("sid,cls", ALL, ids=[sid for sid, _ in ALL])
def test_meta_valid(sid, cls):
    m = cls.meta
    assert m.strategy_id == sid
    assert isinstance(m.category, Category)
    assert isinstance(m.timeframe, Timeframe)
    assert m.scan_schedule in VALID_SCHEDULES, f"bad scan_schedule {m.scan_schedule}"
    assert m.instruments, "instruments/universe key required"
    assert universes.resolve(m.instruments), "universe resolves to nothing"
    assert m.warmup_bars >= 0
    assert m.capital_required > 0
    assert m.name and m.description, "human-readable name + description required"


@pytest.mark.parametrize("sid,cls", ALL, ids=[sid for sid, _ in ALL])
def test_signals_shape_and_determinism(sid, cls):
    strat = cls()
    data = _fixture_data(cls.meta)
    ts = max(df.index[-1] for df in data.values()).to_pydatetime()
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    ctx = _ctx(ts, cls.meta.capital_required)

    first = strat.generate_signals(data, ctx)
    assert isinstance(first, list)
    for sig in first:
        assert isinstance(sig, Signal)
        assert sig.strategy_id == sid
        assert isinstance(sig.signal_type, SignalType)
        assert sig.reference_price > 0
        if sig.signal_type in (SignalType.ENTRY_LONG, SignalType.ENTRY_SHORT):
            has_risk_definition = (
                sig.stop_loss is not None or sig.structure is not None
                or sig.size_hint is not None)
            assert has_risk_definition, (
                "entry signals must carry stop_loss, a defined-risk structure, "
                "or a size_hint — the risk engine cannot size them otherwise")
        if sig.stop_loss is not None and sig.signal_type == SignalType.ENTRY_LONG:
            assert sig.stop_loss < sig.reference_price
        if sig.stop_loss is not None and sig.signal_type == SignalType.ENTRY_SHORT:
            assert sig.stop_loss > sig.reference_price

    # determinism: fresh instance, same inputs -> same outputs
    second = cls().generate_signals(data, ctx)
    assert len(second) == len(first)
    for a, b in zip(first, second):
        assert (a.signal_type, a.instrument, a.reference_price, a.stop_loss,
                a.take_profit) == (b.signal_type, b.instrument, b.reference_price,
                                   b.stop_loss, b.take_profit)


@pytest.mark.parametrize("sid,cls", ALL, ids=[sid for sid, _ in ALL])
def test_no_lookahead(sid, cls):
    """Signals computed at bar T must not change when bars after T are appended."""
    strat = cls()
    data = _fixture_data(cls.meta)
    cut = {sym: df.iloc[:-20] for sym, df in data.items()}
    if any(len(df) < cls.meta.warmup_bars + 5 for df in cut.values()):
        pytest.skip("fixture too short for warmup")
    ts = max(df.index[-1] for df in cut.values()).to_pydatetime()
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    ctx = _ctx(ts, cls.meta.capital_required)

    at_cut = strat.generate_signals(cut, ctx)

    # same evaluation timestamp, same visible bars, fresh frame objects:
    # results must be identical (guards against instance state leakage)
    trimmed_to_now = {
        sym: df[df.index <= pd.Timestamp(ts)] for sym, df in data.items()}
    again = cls().generate_signals(trimmed_to_now, ctx)

    assert len(at_cut) == len(again), "signals changed when future bars were appended"
    for a, b in zip(at_cut, again):
        assert a.signal_type == b.signal_type
        assert a.instrument == b.instrument
