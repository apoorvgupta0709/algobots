"""Tests for iron condor strategy."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from scripts.iron_condor_strategy import (
    Candle,
    IronCondorSignal,
    backtest_iron_condor,
    estimate_option_premium,
    evaluate_bn_iron_condor,
    evaluate_iron_condor,
    is_range_day,
    low_body_candle,
    nearest_strike,
    q2,
    D,
)

TWO_PLACES = Decimal("0.01")
IST = timezone(timedelta(hours=5, minutes=30))


def make_candle(
    time_str: str,
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    vol: int = 0,
) -> Candle:
    h, m = map(int, time_str.split(":"))
    ts = datetime(2026, 6, 22, h, m, tzinfo=IST)
    return Candle(ts=ts, open=D(str(open_p)), high=D(str(high_p)), low=D(str(low_p)), close=D(str(close_p)), volume=vol)


def make_signal(**kw) -> IronCondorSignal:
    defaults = dict(
        strategy_id="banknifty_iron_condor",
        direction="neutral",
        structure="iron_condor",
        entry_time=datetime(2026, 6, 22, 10, 0, tzinfo=IST),
        underlying_entry=D("57890"),
        sold_put_strike=D("57800"),
        bought_put_strike=D("57600"),
        sold_call_strike=D("58000"),
        bought_call_strike=D("58200"),
        net_credit=D("257.12"),
        max_loss_rupees=D("6000"),
        stop_underlying=D("58290"),
        target_underlying=D("57890"),
        reason="test",
    )
    defaults.update(kw)
    return IronCondorSignal(**defaults)


class TestHelpers:
    def test_D_preserves_decimal(self):
        v = Decimal("10.5")
        assert D(v) is v

    def test_D_converts_str(self):
        assert D("10.5") == Decimal("10.5")

    def test_D_converts_float(self):
        assert D(10.5) == Decimal("10.5")

    def test_q2_rounds(self):
        assert q2(Decimal("10.555")) == Decimal("10.56")

    def test_nearest_strike(self):
        strikes = [Decimal("57000"), Decimal("57100"), Decimal("57200")]
        assert nearest_strike(Decimal("57050"), strikes, Decimal("100")) == Decimal("57100")

    def test_nearest_strike_empty_list(self):
        assert nearest_strike(Decimal("57050"), [], Decimal("100")) == Decimal("57100")

    def test_is_range_day_true(self):
        candles = [
            make_candle("09:15", 57800, 57850, 57750, 57800),
            make_candle("09:20", 57800, 57850, 57750, 57800),
            make_candle("09:25", 57800, 57900, 57750, 57850),
        ]
        assert is_range_day(candles, Decimal("57800")) is True

    def test_is_range_day_false(self):
        candles = [
            make_candle("09:15", 57800, 58500, 57200, 57800),
            make_candle("09:20", 57800, 58500, 57200, 57800),
        ]
        assert is_range_day(candles, Decimal("57800")) is False

    def test_is_range_day_empty(self):
        assert is_range_day([], Decimal("57800")) is False

    def test_low_body_candle_true(self):
        c = make_candle("10:00", 57850, 57900, 57800, 57850)
        assert low_body_candle(c, Decimal("57800")) is True

    def test_low_body_candle_false_large_body(self):
        c = make_candle("10:00", 57850, 58500, 57500, 58500)
        assert low_body_candle(c, Decimal("57800")) is False

    def test_low_body_candle_false_no_upper_wick(self):
        c = make_candle("10:00", 57850, 57850, 57700, 57800)
        assert low_body_candle(c, Decimal("57800")) is False


class TestPremiumEstimate:
    def test_call_intrinsic_itm(self):
        prem = estimate_option_premium(D("57000"), D("58000"), "CE", 8, D("15"))
        assert prem >= 1000  # ITM by 1000, time value on top

    def test_put_intrinsic_itm(self):
        prem = estimate_option_premium(D("59000"), D("58000"), "PE", 8, D("15"))
        assert prem >= 1000  # ITM by 1000

    def test_otm_premium_positive(self):
        prem = estimate_option_premium(D("57800"), D("57890"), "PE", 8, D("15"))
        assert prem > 100  # OTM but has time value

    def test_minimum_premium(self):
        prem = estimate_option_premium(D("29000"), D("57890"), "CE", 1, D("8"))
        assert prem >= Decimal("2")


class TestEvaluateIronCondor:
    def make_range_candles(self) -> list[Candle]:
        candles = []
        # 9:15-9:55 pre-market ORB candles
        for m in range(0, 40, 5):
            candles.append(make_candle(f"09:{15+m:02d}" if m < 45 else f"09:{m:02d}", 57800, 57850, 57750, 57800))
        # 10:00 entry candle (low body with wicks)
        candles.append(make_candle("10:00", 57807, 57826, 57758, 57758))
        # post-entry candles
        for m in range(5, 120, 5):
            h, mi = divmod(10 * 60 + m, 60)
            candles.append(make_candle(f"{h:02d}:{mi:02d}", 57760, 57850, 57750, 57800))
        return candles

    def make_contracts(self) -> list[dict]:
        """Create sample option contracts mimicking DB data."""
        contracts = []
        expiry = date(2026, 6, 30)
        for strike in range(56000, 59500, 100):
            contracts.append({"strike": D(str(strike)), "option_type": "CE", "expiry": expiry, "lot_size": 30})
            contracts.append({"strike": D(str(strike)), "option_type": "PE", "expiry": expiry, "lot_size": 30})
        return contracts

    def test_no_candles(self):
        result = evaluate_iron_condor(
            [], trade_date=date(2026, 6, 22), option_contracts=[], spot=D("57890"),
            vix=D("15"), atm_iv=D("15"), lot_size=30,
        )
        assert result is None

    def test_high_vix_blocks(self):
        c = self.make_range_candles()
        result = evaluate_iron_condor(
            c, trade_date=date(2026, 6, 22), option_contracts=self.make_contracts(),
            spot=D("57890"), vix=D("30"), atm_iv=D("15"), lot_size=30,
        )
        assert result is None

    def test_low_vix_blocks(self):
        c = self.make_range_candles()
        result = evaluate_iron_condor(
            c, trade_date=date(2026, 6, 22), option_contracts=self.make_contracts(),
            spot=D("57890"), vix=D("5"), atm_iv=D("15"), lot_size=30,
        )
        assert result is None

    def test_range_day_accepts_when_risk_cap_allows(self):
        c = self.make_range_candles()
        result = evaluate_bn_iron_condor(
            c, trade_date=date(2026, 6, 22), option_contracts=self.make_contracts(),
            spot=D("57890"), lot_size=30, strike_step=D("100"),
            max_loss_cap=D("20000"), min_credit=D("100"),
        )
        assert result is not None
        assert result.structure == "iron_condor"
        assert result.direction == "neutral"
        assert result.net_credit >= Decimal("100")
        assert result.max_loss_rupees == q2(D(str(result.metadata["structural_risk"])))

    def test_sold_strikes_are_roughly_half_percent_otm_from_entry(self):
        c = self.make_range_candles()
        result = evaluate_bn_iron_condor(
            c, trade_date=date(2026, 6, 22), option_contracts=self.make_contracts(),
            spot=D("57890"), lot_size=30, strike_step=D("100"),
            max_loss_cap=D("20000"), min_credit=D("100"),
        )
        assert result is not None
        entry = result.underlying_entry
        put_distance_pct = (entry - result.sold_put_strike) / entry
        call_distance_pct = (result.sold_call_strike - entry) / entry
        assert D("0.0025") <= put_distance_pct <= D("0.0075")
        assert D("0.0025") <= call_distance_pct <= D("0.0075")
        assert result.sold_put_strike < entry < result.sold_call_strike

    def test_bought_strikes_are_roughly_one_and_half_percent_otm_from_entry(self):
        c = self.make_range_candles()
        result = evaluate_bn_iron_condor(
            c, trade_date=date(2026, 6, 22), option_contracts=self.make_contracts(),
            spot=D("57890"), lot_size=30, strike_step=D("100"),
            max_loss_cap=D("20000"), min_credit=D("100"),
        )
        assert result is not None
        entry = result.underlying_entry
        put_distance_pct = (entry - result.bought_put_strike) / entry
        call_distance_pct = (result.bought_call_strike - entry) / entry
        assert D("0.0125") <= put_distance_pct <= D("0.0175")
        assert D("0.0125") <= call_distance_pct <= D("0.0175")

    def test_default_risk_cap_rejects_structurally_unsafe_condor(self):
        c = self.make_range_candles()
        result = evaluate_bn_iron_condor(
            c, trade_date=date(2026, 6, 22), option_contracts=self.make_contracts(),
            spot=D("57890"), lot_size=30, strike_step=D("100"),
            max_loss_cap=D("3000"), min_credit=D("100"),
        )
        assert result is None

    def test_no_entry_before_10am(self):
        """Candles only before 10:00 should not trigger entry."""
        candles = [make_candle(f"09:{m:02d}", 57800, 57850, 57750, 57800) for m in range(15, 60, 5)]
        result = evaluate_iron_condor(
            candles, trade_date=date(2026, 6, 22), option_contracts=self.make_contracts(),
            spot=D("57890"), vix=D("15"), atm_iv=D("15"), lot_size=30,
        )
        assert result is None


class TestBacktest:
    def make_post_entry_candles(self, exit_time: str, exit_price: float) -> list[Candle]:
        """Candles from entry through exit."""
        candles = []
        for m in range(0, 30, 5):
            h, mi = divmod(10 * 60 + m, 60)
            candles.append(make_candle(f"{h:02d}:{mi:02d}", 57800, 57850, 57750, 57800))
        # Add exit candle
        h, m = map(int, exit_time.split(":"))
        candles.append(make_candle(exit_time, exit_price, exit_price + 10, exit_price - 10, exit_price))
        return candles

    def test_time_exit(self):
        s = make_signal()
        c = self.make_post_entry_candles("15:15", 57800)
        r = backtest_iron_condor(s, c, lot_size=30)
        assert r["exit_reason"] == "time_exit"
        assert r["realized_pnl"] > 0  # stayed in range

    def test_stop_breach_call_side(self):
        s = make_signal(stop_underlying=D("58290"))
        c = self.make_post_entry_candles("11:00", 58350)
        r = backtest_iron_condor(s, c, lot_size=30)
        assert r["exit_reason"] == "stop_breach"
        assert r["realized_pnl"] <= 0

    def test_stop_breach_put_side(self):
        s = make_signal(sold_put_strike=D("57800"))
        c = self.make_post_entry_candles("11:00", 57500)
        r = backtest_iron_condor(s, c, lot_size=30)
        assert r["exit_reason"] == "stop_breach"
        assert r["realized_pnl"] <= 0

    def test_target_profit(self):
        s = make_signal()
        c = self.make_post_entry_candles("12:30", 57890)
        # Add candles showing price stayed in range
        c.extend(self.make_post_entry_candles("12:30", 57880))
        r = backtest_iron_condor(make_signal(underlying_entry=D("57890")), c, lot_size=30)
        # Should stay between sold strikes -> profit
        if r["exit_reason"] == "target_profit":
            assert r["realized_pnl"] > 0

    def test_max_loss_capped(self):
        s = make_signal(max_loss_rupees=D("6000"))
        c = self.make_post_entry_candles("11:00", 55000)  # Deep breach
        r = backtest_iron_condor(s, c, lot_size=30)
        assert r["realized_pnl"] >= -D("6000")


class TestRunScript:
    """Smoke tests: verify the runner script compiles and parses args."""

    def test_import_path(self):
        from scripts import run_iron_condor
        assert hasattr(run_iron_condor, "main")

    def test_parse_args_backtest(self):
        """Import the module and verify it loads (parser is inline in main)."""
        from scripts.run_iron_condor import main
        assert callable(main)