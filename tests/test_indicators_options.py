"""Tests for the indicators and options toolkits."""
from __future__ import annotations

import datetime as dt
import math
import sys
import types

import numpy as np
import pandas as pd
import pytest

from algobot.core.enums import OptionType, Side
from algobot.core.models import ExpiryRule, StrikeRule
from algobot.indicators import fundamentals as fnd
from algobot.indicators.momentum import momentum_12_1, rsi
from algobot.indicators.trend import crossover, ema, supertrend
from algobot.indicators.volatility import atr, bollinger
from algobot.indicators.volume import vwap
from algobot.options.chain import OptionChain
from algobot.options.leg_builder import LegBuilder
from algobot.options.margin import estimate_margin
from algobot.options.pricing import bs_price, implied_vol, synthetic_premium_series
from algobot.options.structures import (
    covered_call,
    iron_condor,
    long_option,
    vertical_spread,
)

IST = "Asia/Kolkata"
NIFTY = "NSE:NIFTY50-INDEX"


# ------------------------------------------------------------------ fixtures
def _random_walk_ohlc(n: int = 500, seed: int = 7, start: float = 24000.0) -> pd.DataFrame:
    """Seeded random-walk OHLCV frame with a daily-frequency index."""
    rng = np.random.default_rng(seed)
    close = start * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    spread = np.abs(rng.normal(0, 0.004, n)) * close
    high = close + spread
    low = close - spread
    open_ = np.roll(close, 1)
    open_[0] = start
    volume = rng.integers(10_000, 100_000, n).astype(float)
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz=IST)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _intraday_two_days() -> pd.DataFrame:
    """Two 5-minute sessions (09:15-15:25 IST)."""
    rng = np.random.default_rng(42)
    frames = []
    for day in ("2026-06-01", "2026-06-02"):
        idx = pd.date_range(f"{day} 09:15", f"{day} 15:25", freq="5min", tz=IST)
        close = 24000 + np.cumsum(rng.normal(0, 8, len(idx)))
        frames.append(
            pd.DataFrame(
                {
                    "open": close,
                    "high": close + 5,
                    "low": close - 5,
                    "close": close,
                    "volume": rng.integers(1_000, 5_000, len(idx)).astype(float),
                },
                index=idx,
            )
        )
    return pd.concat(frames)


# ---------------------------------------------------------------- indicators
class TestIndicators:
    def test_ema_warmup_and_convergence(self):
        s = pd.Series([100.0] * 60)
        e = ema(s, 20)
        assert e.iloc[:19].isna().all()
        assert e.iloc[-1] == pytest.approx(100.0)

    def test_rsi_bounds_and_edges(self):
        df = _random_walk_ohlc()
        r = rsi(df["close"], 14)
        valid = r.dropna()
        assert ((valid >= 0) & (valid <= 100)).all()
        assert r.iloc[:13].isna().all()  # warmup
        # monotone rise -> RSI 100, flat -> neutral 50
        assert rsi(pd.Series(np.arange(50.0)), 14).iloc[-1] == pytest.approx(100.0)
        assert rsi(pd.Series([100.0] * 50), 14).iloc[-1] == pytest.approx(50.0)

    def test_atr_positive_after_warmup(self):
        df = _random_walk_ohlc()
        a = atr(df, 14)
        assert a.iloc[:13].isna().all()
        assert (a.dropna() > 0).all()

    def test_bollinger_ordering(self):
        df = _random_walk_ohlc()
        bb = bollinger(df["close"], 20, 2.0)
        v = bb.dropna()
        assert (v["upper"] >= v["mid"]).all()
        assert (v["mid"] >= v["lower"]).all()
        assert (v["width"] > 0).all()

    def test_vwap_resets_daily(self):
        df = _intraday_two_days()
        v = vwap(df)
        first_bar_d2 = df[df.index.date == dt.date(2026, 6, 2)].iloc[0]
        tp = (first_bar_d2["high"] + first_bar_d2["low"] + first_bar_d2["close"]) / 3
        assert v[df.index.date == dt.date(2026, 6, 2)].iloc[0] == pytest.approx(tp)
        # zero-volume feed falls back to equal weight, still finite
        df0 = df.assign(volume=0.0)
        assert vwap(df0).notna().all()

    def test_supertrend_direction_flips(self):
        n = 80
        up = np.linspace(100, 200, n)
        down = np.linspace(200, 100, n)
        close = np.concatenate([up, down])
        df = pd.DataFrame(
            {"high": close + 1, "low": close - 1, "close": close},
            index=pd.date_range("2024-01-01", periods=2 * n, freq="B", tz=IST),
        )
        st = supertrend(df, period=10, mult=3.0)
        d = st["direction"].dropna()
        assert set(d.unique()) == {1.0, -1.0}
        assert d.iloc[50] == 1.0     # deep in the uptrend
        assert d.iloc[-1] == -1.0    # deep in the downtrend
        assert st["st"].dropna().notna().all()

    def test_crossover_flags(self):
        fast = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0, 3.0])
        slow = pd.Series([2.0] * 6)
        x = crossover(fast, slow)
        assert list(x) == [0, 0, 1, 0, -1, 1]

    def test_momentum_12_1(self):
        assert math.isnan(momentum_12_1(pd.Series(np.arange(100.0) + 1)))
        c = pd.Series(np.linspace(100, 200, 300))
        m = momentum_12_1(c)
        assert m == pytest.approx(c.iloc[-21] / c.iloc[-252] - 1)


# ------------------------------------------------------------------- pricing
class TestPricing:
    def test_put_call_parity(self):
        s, k, t, iv, r = 24500.0, 24600.0, 30 / 365, 0.14, 0.065
        c = bs_price(s, k, t, iv, "CE", r)
        p = bs_price(s, k, t, iv, "PE", r)
        assert c - p == pytest.approx(s - k * math.exp(-r * t), abs=1e-6)

    def test_intrinsic_at_expiry(self):
        assert bs_price(24500, 24000, 0.0, 0.14, "CE") == pytest.approx(500.0)
        assert bs_price(24500, 24000, 0.0, 0.14, "PE") == pytest.approx(0.0)
        assert bs_price(24500, 25000, -0.01, 0.14, "PE") == pytest.approx(500.0)

    def test_implied_vol_round_trip(self):
        s, k, t = 24500.0, 24700.0, 21 / 365
        price = bs_price(s, k, t, 0.18, "CE")
        assert implied_vol(price, s, k, t, "CE") == pytest.approx(0.18, abs=1e-4)

    def test_synthetic_premium_series_decays_and_settles(self):
        idx = pd.date_range("2026-06-01 09:15", periods=5, freq="1D", tz=IST)
        spot = pd.Series(24500.0, index=idx)
        expiry = pd.Timestamp("2026-06-03 15:30", tz=IST)
        prem = synthetic_premium_series(spot, 24500.0, expiry, "CE", 0.14)
        assert prem.iloc[0] > prem.iloc[1] > prem.iloc[2] > 0  # theta decay
        assert prem.iloc[3] == pytest.approx(0.0)              # post expiry: intrinsic


# --------------------------------------------------------------------- chain
class TestChain:
    NOW = dt.datetime(2026, 6, 29, 10, 0)  # Monday morning IST (naive -> IST)
    EXPIRY = dt.date(2026, 7, 2)           # Thursday

    def test_atm_and_delta_50(self):
        chain = OptionChain.synthetic(NIFTY, 24512.0, self.NOW)
        atm = chain.atm_strike()
        assert atm == 24500.0
        k = chain.strike_by_delta(0.50, "CE", self.EXPIRY)
        assert abs(k - atm) <= 50.0  # within one strike step of ATM

    def test_strike_by_delta_degeneracy_guard(self):
        # 16:00 on expiry day -> past the 15:30 IST cutoff -> t == 0
        now = dt.datetime(2026, 7, 2, 16, 0)
        chain = OptionChain.synthetic(NIFTY, 24512.0, now)
        assert chain.strike_by_delta(0.20, "CE", self.EXPIRY) == chain.atm_strike()
        assert chain.strike_by_delta(0.20, "PE", self.EXPIRY) == chain.atm_strike()

    def test_premium_pct_and_quotes_fallback(self):
        chain = OptionChain.synthetic(NIFTY, 24500.0, self.NOW)
        k = chain.strike_by_premium_pct(0.5, "CE", self.EXPIRY)
        target = 24500.0 * 0.005
        assert abs(chain.premium(k, "CE", self.EXPIRY) - target) < target  # sane pick
        q = OptionChain.from_quotes(NIFTY, 24500.0, self.NOW, {"24500CE": 123.45})
        assert q.premium(24500.0, "CE", self.EXPIRY) == pytest.approx(123.45)
        # missing strike -> synthetic fallback, still a positive premium
        assert q.premium(24600.0, "CE", self.EXPIRY) > 0


# --------------------------------------------------------- leg builder stubs
def _install_data_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub algobot.data.{expiries,instruments} with the documented signatures."""
    expiries = types.ModuleType("algobot.data.expiries")

    def next_expiry(root: str, kind: str, n: int, on_date: dt.date) -> dt.date:
        thursday = on_date + dt.timedelta(days=(3 - on_date.weekday()) % 7)
        if kind == "weekly":
            return thursday + dt.timedelta(weeks=n)
        return thursday + dt.timedelta(weeks=4 * (n + 1))

    expiries.next_expiry = next_expiry

    instruments = types.ModuleType("algobot.data.instruments")
    instruments.option_symbol = (
        lambda root, expiry, strike, opt_type:
        f"NSE:{root}{expiry:%y%m%d}{int(strike)}{opt_type}"
    )
    instruments.future_symbol = lambda root, expiry: f"NSE:{root}{expiry:%y%b}FUT"
    instruments.root_of = lambda underlying: (
        "BANKNIFTY" if "BANK" in underlying.upper() else "NIFTY"
    )

    monkeypatch.setitem(sys.modules, "algobot.data.expiries", expiries)
    monkeypatch.setitem(sys.modules, "algobot.data.instruments", instruments)


class TestLegBuilder:
    NOW = dt.datetime(2026, 6, 29, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))

    def test_iron_condor_resolution(self, monkeypatch):
        _install_data_stubs(monkeypatch)
        spot = 24512.0
        structure = iron_condor(NIFTY, short_delta=0.20, wing_steps=4)
        resolved = LegBuilder().resolve(structure, spot, self.NOW)

        assert resolved is not structure  # deep copy
        assert all(leg.resolved_strike is None for leg in structure.legs)
        assert len(resolved.legs) == 4
        for leg in resolved.legs:
            assert leg.resolved_symbol and leg.resolved_symbol.startswith("NSE:NIFTY")
            assert leg.resolved_strike is not None
            assert dt.date.fromisoformat(leg.resolved_expiry) == dt.date(2026, 7, 2)

        short_ce, wing_ce, short_pe, wing_pe = resolved.legs
        assert wing_ce.resolved_strike - short_ce.resolved_strike == pytest.approx(200.0)
        assert short_pe.resolved_strike - wing_pe.resolved_strike == pytest.approx(200.0)
        assert short_ce.resolved_strike > 24512.0 > short_pe.resolved_strike  # OTM shorts
        assert str(int(wing_ce.resolved_strike)) in wing_ce.resolved_symbol

    def test_rule_methods(self, monkeypatch):
        _install_data_stubs(monkeypatch)
        builder = LegBuilder()
        spot = 24512.0
        atm_call = long_option(NIFTY, OptionType.CE, StrikeRule.atm(2))
        assert builder.resolve(atm_call, spot, self.NOW).legs[0].resolved_strike == 24600.0
        absolute = long_option(NIFTY, OptionType.PE, StrikeRule.absolute(24000.0))
        assert builder.resolve(absolute, spot, self.NOW).legs[0].resolved_strike == 24000.0
        pct = long_option(NIFTY, OptionType.PE, StrikeRule.pct_otm(2.0))
        assert builder.resolve(pct, spot, self.NOW).legs[0].resolved_strike == pytest.approx(
            round(24512.0 * 0.98 / 50) * 50
        )


# -------------------------------------------------------------------- margin
class TestMargin:
    SPOT, LOT = 24500.0, 65

    def test_naked_short(self):
        s = covered_call(NIFTY, StrikeRule.absolute(24700.0))
        assert estimate_margin(s, self.SPOT, self.LOT) == pytest.approx(
            0.13 * self.SPOT * self.LOT
        )

    def test_long_option_zero(self):
        s = long_option(NIFTY, OptionType.CE, StrikeRule.absolute(24500.0))
        assert estimate_margin(s, self.SPOT, self.LOT) == pytest.approx(0.0)

    def test_credit_spread_width(self):
        s = vertical_spread(
            NIFTY, OptionType.CE, "credit",
            buy_rule=StrikeRule.absolute(24700.0),
            sell_rule=StrikeRule.absolute(24500.0),
        )
        assert estimate_margin(s, self.SPOT, self.LOT) == pytest.approx(200.0 * self.LOT)

    def test_iron_condor_max_side(self, monkeypatch):
        _install_data_stubs(monkeypatch)
        now = dt.datetime(2026, 6, 29, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
        resolved = LegBuilder().resolve(iron_condor(NIFTY, wing_steps=4), self.SPOT, now)
        # both sides are 4-step (200 pt) spreads -> margin is one side, not two
        assert estimate_margin(resolved, self.SPOT, self.LOT) == pytest.approx(200.0 * self.LOT)


# -------------------------------------------------------------- fundamentals
class TestFundamentals:
    def test_csv_load_and_symbol_normalization(self):
        provider = fnd.CsvFundamentals()
        df = provider.get(["NSE:SBIN-EQ", "NSE:MARUTI-EQ", "NSE:NOSUCHSTOCK-EQ"])
        assert list(df.index) == ["NSE:SBIN-EQ", "NSE:MARUTI-EQ", "NSE:NOSUCHSTOCK-EQ"]
        assert df.loc["NSE:SBIN-EQ", "pe"] == pytest.approx(9.8)
        assert df.loc["NSE:NOSUCHSTOCK-EQ"].isna().all()

    def test_graham_style_screen(self):
        provider = fnd.CsvFundamentals()
        df = provider.get([f"NSE:{s}-EQ" for s in ["SBIN", "RELIANCE", "TCS", "TITAN"]])
        out = fnd.screen(
            df,
            {
                "pe": ("<", 14),
                "pb": ("<=", 1.5),
                "de_ratio": ("<=", 0.1),
                "dividend_yield": (">", 1.0),
                "promoter_pledge": ("==", 0.0),
            },
        )
        assert list(out.index) == ["NSE:SBIN-EQ"]

    def test_nan_never_passes(self):
        df = pd.DataFrame({c: [float("nan")] for c in fnd.FUND_COLUMNS}, index=["X"])
        assert fnd.screen(df, {"pe": ("!=", 14)}).empty
        assert fnd.screen(df, {"pe": ("<", 1e9)}).empty

    def test_missing_file_yields_empty_typed_frame(self, tmp_path):
        provider = fnd.CsvFundamentals(path=str(tmp_path / "nope.csv"))
        df = provider.get(["NSE:SBIN-EQ"])
        assert list(df.columns) == fnd.FUND_COLUMNS
        assert df.loc["NSE:SBIN-EQ"].isna().all()
        assert fnd.screen(df, {"pe": ("<", 10)}).empty
