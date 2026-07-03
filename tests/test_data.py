"""Tests for the data layer: expiries, instruments, feed normalization, cache.

No network: Fyers is faked, candles are synthetic. Known-date facts used
below (2026, IST): 2026-07-01 is a Wednesday; 2026-07-07 / 07-14 / 07-28 are
Tuesdays (07-28 is the last Tuesday of July); 2026-04-14 (Ambedkar Jayanti)
and 2026-11-24 (Gurunanak Jayanti) are Tuesday NSE holidays, 11-24 being the
last Tuesday of November.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from algobot.core.enums import Timeframe
from algobot.core.exceptions import DataError
from algobot.data import expiries, instruments
from algobot.data.cache import CachedFeed, CandleCache
from algobot.data.corporate_actions import adjust, load_actions
from algobot.data.feed import CANDLE_COLUMNS, TZ, DataFeed, normalize_candles
from algobot.data.fyers_feed import FyersFeed

IST = TZ


# --------------------------------------------------------------------- helpers
def make_daily_df(start: dt.date, days: int, base: float = 100.0) -> pd.DataFrame:
    """Synthetic canonical daily candles: one bar per calendar day at 09:15 IST."""
    idx = pd.date_range(start=pd.Timestamp(start, tz=IST) + pd.Timedelta(hours=9, minutes=15),
                        periods=days, freq="D", name="ts")
    close = pd.Series(range(days), index=idx, dtype=float) + base
    return pd.DataFrame({"open": close - 1, "high": close + 2,
                         "low": close - 2, "close": close,
                         "volume": 1000.0}, index=idx)


class FakeFyers:
    """Duck-typed fyers_apiv3 FyersModel for history/quotes."""

    def __init__(self, candles=None, history_status="ok", quotes_payload=None):
        self.candles = candles or []
        self.history_status = history_status
        self.quotes_payload = quotes_payload
        self.history_calls: list[dict] = []

    def history(self, data):
        self.history_calls.append(data)
        if self.history_status != "ok":
            return {"s": self.history_status, "message": "boom"}
        return {"s": "ok", "candles": self.candles}

    def quotes(self, data):
        return self.quotes_payload or {
            "s": "ok",
            "d": [{"n": "NSE:SBIN-EQ", "v": {"lp": 812.5}},
                  {"n": "NSE:NIFTY50-INDEX", "v": {"lp": 24512.35}}],
        }


class FakeInnerFeed(DataFeed):
    """Records get_candles calls; serves synthetic daily bars for the range."""

    def __init__(self):
        self.calls: list[tuple[dt.date, dt.date]] = []

    def get_candles(self, symbol, timeframe, start, end):
        self.calls.append((start, end))
        return make_daily_df(start, (end - start).days + 1)

    def get_quotes(self, symbols):
        return {s: 100.0 for s in symbols}


# -------------------------------------------------------------------- expiries
class TestExpiries:
    def test_next_weekly_nifty_from_wednesday_is_next_tuesday(self):
        # Wed 2026-07-01 -> Tue 2026-07-07
        assert expiries.next_expiry("NIFTY", "weekly", 0,
                                    dt.date(2026, 7, 1)) == dt.date(2026, 7, 7)

    def test_weekly_n1_is_following_tuesday(self):
        assert expiries.next_expiry("NIFTY", "weekly", 1,
                                    dt.date(2026, 7, 1)) == dt.date(2026, 7, 14)

    def test_weekly_on_expiry_day_returns_same_day(self):
        assert expiries.next_expiry("NIFTY", "weekly", 0,
                                    dt.date(2026, 7, 7)) == dt.date(2026, 7, 7)

    def test_monthly_is_last_tuesday(self):
        assert expiries.monthly_expiry("NIFTY", 2026, 7) == dt.date(2026, 7, 28)
        assert expiries.next_expiry("NIFTY", "monthly", 0,
                                    dt.date(2026, 7, 1)) == dt.date(2026, 7, 28)

    def test_weekly_holiday_shifts_to_previous_trading_day(self):
        # Tue 2026-04-14 is a holiday -> Mon 2026-04-13
        assert expiries.next_expiry("NIFTY", "weekly", 0,
                                    dt.date(2026, 4, 9)) == dt.date(2026, 4, 13)

    def test_monthly_holiday_shifts_to_previous_trading_day(self):
        # last Tuesday of Nov 2026 (11-24) is a holiday -> Mon 2026-11-23
        assert expiries.monthly_expiry("NIFTY", 2026, 11) == dt.date(2026, 11, 23)

    def test_banknifty_weekly_resolves_to_monthly(self):
        assert expiries.next_expiry("BANKNIFTY", "weekly", 0,
                                    dt.date(2026, 7, 1)) == dt.date(2026, 7, 28)
        assert expiries.next_expiry("FINNIFTY", "weekly", 0,
                                    dt.date(2026, 7, 1)) == dt.date(2026, 7, 28)

    def test_is_expiry_day(self):
        assert expiries.is_expiry_day("NIFTY", dt.date(2026, 7, 7))
        assert not expiries.is_expiry_day("NIFTY", dt.date(2026, 7, 8))
        assert expiries.is_expiry_day("BANKNIFTY", dt.date(2026, 7, 28))
        assert not expiries.is_expiry_day("BANKNIFTY", dt.date(2026, 7, 7))

    def test_days_to_expiry(self):
        assert expiries.days_to_expiry("NIFTY", "weekly", dt.date(2026, 7, 1)) == 6
        assert expiries.days_to_expiry("NIFTY", "weekly", dt.date(2026, 7, 7)) == 0
        assert expiries.days_to_expiry("NIFTY", "monthly", dt.date(2026, 7, 1)) == 27

    def test_bad_kind_rejected(self):
        with pytest.raises(ValueError):
            expiries.next_expiry("NIFTY", "fortnightly", 0, dt.date(2026, 7, 1))


# ----------------------------------------------------------------- instruments
class TestInstruments:
    def test_root_of(self):
        assert instruments.root_of("NSE:NIFTY50-INDEX") == "NIFTY"
        assert instruments.root_of("NSE:NIFTYBANK-INDEX") == "BANKNIFTY"
        assert instruments.root_of("NSE:FINNIFTY-INDEX") == "FINNIFTY"
        assert instruments.root_of("NSE:SBIN-EQ") == "SBIN"

    def test_underlying_of_roundtrip(self):
        for sym in ("NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX",
                    "NSE:FINNIFTY-INDEX", "NSE:SBIN-EQ"):
            assert instruments.underlying_of(instruments.root_of(sym)) == sym

    def test_monthly_option_symbol(self):
        sym = instruments.option_symbol("NIFTY", dt.date(2026, 7, 28), 24500, "CE")
        assert sym == "NSE:NIFTY26JUL24500CE"
        assert instruments.option_symbol("BANKNIFTY", dt.date(2026, 7, 28), 57000, "PE") \
            == "NSE:BANKNIFTY26JUL57000PE"

    def test_weekly_option_symbol(self):
        assert instruments.option_symbol("NIFTY", dt.date(2026, 7, 7), 24500, "CE") \
            == "NSE:NIFTY2670724500CE"
        # Oct/Nov/Dec use O/N/D month codes
        assert instruments.option_symbol("NIFTY", dt.date(2026, 10, 6), 24500, "PE") \
            == "NSE:NIFTY26O0624500PE"
        # holiday-shifted weekly (nominal Tue 04-14 -> Mon 04-13)
        assert instruments.option_symbol("NIFTY", dt.date(2026, 4, 13), 24500, "CE") \
            == "NSE:NIFTY2641324500CE"

    def test_future_symbol(self):
        assert instruments.future_symbol("NIFTY", dt.date(2026, 7, 28)) \
            == "NSE:NIFTY26JULFUT"

    def test_parse_monthly_roundtrip(self):
        parsed = instruments.parse_option_symbol("NSE:NIFTY26JUL24500CE")
        assert parsed == {"root": "NIFTY", "expiry": dt.date(2026, 7, 28),
                          "strike": 24500.0, "opt_type": "CE"}
        rebuilt = instruments.option_symbol(parsed["root"], parsed["expiry"],
                                            parsed["strike"], parsed["opt_type"])
        assert rebuilt == "NSE:NIFTY26JUL24500CE"

    def test_parse_weekly_roundtrip(self):
        for sym in ("NSE:NIFTY2670724500CE", "NSE:NIFTY26O0624500PE"):
            parsed = instruments.parse_option_symbol(sym)
            rebuilt = instruments.option_symbol(parsed["root"], parsed["expiry"],
                                                parsed["strike"], parsed["opt_type"])
            assert rebuilt == sym
        parsed = instruments.parse_option_symbol("NSE:NIFTY2670724500CE")
        assert parsed["expiry"] == dt.date(2026, 7, 7)
        assert parsed["root"] == "NIFTY"
        assert parsed["strike"] == 24500.0

    def test_parse_rejects_garbage(self):
        with pytest.raises(DataError):
            instruments.parse_option_symbol("NSE:SBIN-EQ")
        with pytest.raises(DataError):
            instruments.parse_option_symbol("NSE:NIFTYFUT")


# ------------------------------------------------------------------- normalize
class TestNormalize:
    def test_normalize_sorts_dedupes_and_localizes(self):
        naive = pd.DataFrame({
            "ts": [dt.datetime(2026, 7, 2, 9, 15), dt.datetime(2026, 7, 1, 9, 15),
                   dt.datetime(2026, 7, 2, 9, 15)],
            "open": [2.0, 1.0, 20.0], "high": [2.0, 1.0, 20.0],
            "low": [2.0, 1.0, 20.0], "close": [2.0, 1.0, 20.0],
            "volume": [10, 10, 10],
        })
        out = normalize_candles(naive)
        assert list(out.columns) == CANDLE_COLUMNS
        assert out.index.name == "ts"
        assert str(out.index.tz) in ("Asia/Kolkata", "pytz.FixedOffset(330)")
        assert out.index.is_monotonic_increasing
        assert len(out) == 2                        # duplicate dropped
        assert out["close"].iloc[-1] == 20.0        # newest wins

    def test_normalize_missing_columns_raises(self):
        df = make_daily_df(dt.date(2026, 7, 1), 3).drop(columns=["volume"])
        with pytest.raises(DataError):
            normalize_candles(df)


# ------------------------------------------------------------------ fyers feed
class TestFyersFeed:
    def _epoch(self, y, m, d, hh=9, mm=15):
        return int(pd.Timestamp(dt.datetime(y, m, d, hh, mm), tz=IST).timestamp())

    def test_get_candles_shape_and_payload(self):
        candles = [[self._epoch(2026, 7, 1), 100, 102, 99, 101, 5000],
                   [self._epoch(2026, 7, 2), 101, 103, 100, 102, 6000]]
        fake = FakeFyers(candles=candles)
        feed = FyersFeed(fake, chunk_pause=0)
        df = feed.get_candles("NSE:NIFTY50-INDEX", Timeframe.DAY,
                              dt.date(2026, 7, 1), dt.date(2026, 7, 2))
        assert list(df.columns) == CANDLE_COLUMNS
        assert df.index.name == "ts" and df.index.tz is not None
        assert len(df) == 2 and df["close"].tolist() == [101.0, 102.0]
        assert df.index[0].hour == 9 and df.index[0].minute == 15  # IST wall clock
        payload = fake.history_calls[0]
        assert payload["symbol"] == "NSE:NIFTY50-INDEX"
        assert payload["resolution"] == "D"
        assert payload["date_format"] == "1"
        assert payload["cont_flag"] == "1"

    def test_long_range_is_chunked(self):
        candles = [[self._epoch(2026, 1, 5), 1, 2, 0.5, 1.5, 10]]
        fake = FakeFyers(candles=candles)
        feed = FyersFeed(fake, chunk_pause=0)
        feed.get_candles("NSE:SBIN-EQ", Timeframe.MIN5,
                         dt.date(2026, 1, 1), dt.date(2026, 7, 1))   # 182 days
        assert len(fake.history_calls) == 3                          # 90+90+2
        # chunks tile the range without gaps
        froms = [c["range_from"] for c in fake.history_calls]
        tos = [c["range_to"] for c in fake.history_calls]
        assert froms[0] == "2026-01-01" and tos[-1] == "2026-07-01"

    def test_bad_status_raises(self):
        feed = FyersFeed(FakeFyers(history_status="error"), chunk_pause=0)
        with pytest.raises(DataError):
            feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                             dt.date(2026, 7, 1), dt.date(2026, 7, 2))

    def test_empty_candles_raise(self):
        feed = FyersFeed(FakeFyers(candles=[]), chunk_pause=0)
        with pytest.raises(DataError):
            feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                             dt.date(2026, 7, 1), dt.date(2026, 7, 2))

    def test_quotes(self):
        feed = FyersFeed(FakeFyers(), chunk_pause=0)
        quotes = feed.get_quotes(["NSE:SBIN-EQ", "NSE:NIFTY50-INDEX"])
        assert quotes == {"NSE:SBIN-EQ": 812.5, "NSE:NIFTY50-INDEX": 24512.35}

    def test_quotes_bad_status_raises(self):
        feed = FyersFeed(FakeFyers(quotes_payload={"s": "error"}), chunk_pause=0)
        with pytest.raises(DataError):
            feed.get_quotes(["NSE:SBIN-EQ"])


# ----------------------------------------------------------------------- cache
class TestCandleCache:
    def test_write_read_roundtrip(self, tmp_path):
        cache = CandleCache(tmp_path)
        df = make_daily_df(dt.date(2026, 6, 1), 5)
        cache.write("NSE:NIFTY50-INDEX", Timeframe.DAY, df)
        assert (tmp_path / "NSE_NIFTY50-INDEX" / "D.parquet").exists()
        got = cache.read("NSE:NIFTY50-INDEX", Timeframe.DAY)
        assert got is not None and len(got) == 5
        assert list(got.columns) == CANDLE_COLUMNS
        pd.testing.assert_index_equal(got.index, df.index)

    def test_read_missing_returns_none(self, tmp_path):
        cache = CandleCache(tmp_path)
        assert cache.read("NSE:SBIN-EQ", Timeframe.DAY) is None
        assert cache.last_ts("NSE:SBIN-EQ", Timeframe.DAY) is None

    def test_merge_dedupes_newest_wins(self, tmp_path):
        cache = CandleCache(tmp_path)
        cache.write("NSE:SBIN-EQ", Timeframe.DAY, make_daily_df(dt.date(2026, 6, 1), 5))
        # overlapping rewrite: days 4..8 with different values
        cache.write("NSE:SBIN-EQ", Timeframe.DAY,
                    make_daily_df(dt.date(2026, 6, 4), 5, base=500.0))
        got = cache.read("NSE:SBIN-EQ", Timeframe.DAY)
        assert len(got) == 8                               # 1..8, deduped
        assert got.index.is_monotonic_increasing
        overlap_ts = pd.Timestamp(dt.date(2026, 6, 4), tz=IST) \
            + pd.Timedelta(hours=9, minutes=15)
        assert got.loc[overlap_ts, "close"] == 500.0       # newest wins
        assert cache.last_ts("NSE:SBIN-EQ", Timeframe.DAY) == got.index[-1]


class TestCachedFeed:
    def test_cache_only_mode_serves_and_slices(self, tmp_path):
        cache = CandleCache(tmp_path)
        cache.write("NSE:SBIN-EQ", Timeframe.DAY, make_daily_df(dt.date(2026, 6, 1), 10))
        feed = CachedFeed(None, cache)
        df = feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                              dt.date(2026, 6, 3), dt.date(2026, 6, 5))
        assert len(df) == 3
        assert df.index[0].date() == dt.date(2026, 6, 3)
        assert df.index[-1].date() == dt.date(2026, 6, 5)

    def test_cache_only_mode_raises_when_empty(self, tmp_path):
        feed = CachedFeed(None, CandleCache(tmp_path))
        with pytest.raises(DataError):
            feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                             dt.date(2026, 6, 1), dt.date(2026, 6, 5))
        with pytest.raises(DataError):
            feed.get_quotes(["NSE:SBIN-EQ"])

    def test_first_fetch_populates_cache(self, tmp_path):
        inner = FakeInnerFeed()
        feed = CachedFeed(inner, CandleCache(tmp_path))
        df = feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                              dt.date(2026, 6, 1), dt.date(2026, 6, 10))
        assert inner.calls == [(dt.date(2026, 6, 1), dt.date(2026, 6, 10))]
        assert len(df) == 10
        assert feed.cache.read("NSE:SBIN-EQ", Timeframe.DAY) is not None

    def test_covered_request_hits_cache_only(self, tmp_path):
        inner = FakeInnerFeed()
        feed = CachedFeed(inner, CandleCache(tmp_path))
        feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                         dt.date(2026, 6, 1), dt.date(2026, 6, 10))
        inner.calls.clear()
        df = feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                              dt.date(2026, 6, 2), dt.date(2026, 6, 9))
        assert inner.calls == []                     # served from cache
        assert len(df) == 8

    def test_incremental_tail_and_head_fetch(self, tmp_path):
        inner = FakeInnerFeed()
        feed = CachedFeed(inner, CandleCache(tmp_path))
        feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                         dt.date(2026, 6, 5), dt.date(2026, 6, 10))
        inner.calls.clear()
        df = feed.get_candles("NSE:SBIN-EQ", Timeframe.DAY,
                              dt.date(2026, 6, 1), dt.date(2026, 6, 15))
        # head before cache + tail from last cached day onward
        assert (dt.date(2026, 6, 1), dt.date(2026, 6, 5)) in inner.calls
        assert (dt.date(2026, 6, 10), dt.date(2026, 6, 15)) in inner.calls
        assert len(inner.calls) == 2
        assert len(df) == 15
        assert df.index.is_monotonic_increasing
        assert not df.index.duplicated().any()

    def test_quotes_delegate_to_inner(self, tmp_path):
        feed = CachedFeed(FakeInnerFeed(), CandleCache(tmp_path))
        assert feed.get_quotes(["NSE:SBIN-EQ"]) == {"NSE:SBIN-EQ": 100.0}


# ----------------------------------------------------------- corporate actions
class TestCorporateActions:
    def test_adjust_back_adjusts_before_ex_date(self):
        df = make_daily_df(dt.date(2026, 6, 1), 4, base=100.0)
        out = adjust(df, [{"symbol": "NSE:SBIN-EQ", "date": dt.date(2026, 6, 3),
                           "kind": "split", "ratio": 2.0}])
        assert out["close"].iloc[0] == pytest.approx(50.0)    # halved
        assert out["volume"].iloc[0] == pytest.approx(2000.0)  # doubled
        assert out["close"].iloc[2] == pytest.approx(102.0)   # ex-date on: untouched
        assert df["close"].iloc[0] == pytest.approx(100.0)    # non-mutating

    def test_load_actions_missing_file_is_empty(self, tmp_path):
        assert load_actions(tmp_path / "nope.csv") == []

    def test_load_actions_reads_csv(self, tmp_path):
        path = tmp_path / "corporate_actions.csv"
        path.write_text("symbol,date,kind,ratio\n"
                        "NSE:SBIN-EQ,2026-06-03,split,2\n"
                        "NSE:INFY-EQ,2026-05-01,bonus,1.5\n")
        actions = load_actions(path)
        assert len(actions) == 2
        assert actions[0] == {"symbol": "NSE:SBIN-EQ", "date": dt.date(2026, 6, 3),
                              "kind": "split", "ratio": 2.0}
        assert load_actions(path, symbol="NSE:INFY-EQ")[0]["symbol"] == "NSE:INFY-EQ"
