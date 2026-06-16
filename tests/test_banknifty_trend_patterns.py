from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.banknifty_trend_patterns import (
    BankNiftyDayFeatures,
    Candle,
    PatternClassification,
    build_day_features,
    classify_day_rules,
    close_location,
    cumulative_vwap_series,
    feature_vector,
    find_nearest_similar_days,
    realized_vol_5m,
    summarize_playbook,
    vwap_cross_count,
)

IST = ZoneInfo("Asia/Kolkata")
CONFIG = json.loads((PROJECT_ROOT / "config" / "banknifty_trend_patterns.json").read_text())


def _candle(ts: datetime, o, h, l, c, v=1000) -> Candle:
    return Candle(ts=ts, open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(l)), close=Decimal(str(c)), volume=v)


def _session(prices, *, start=(9, 15), step_min=5, base=Decimal("50000")) -> list[Candle]:
    """Build a 5m session from a list of close prices; OHLC derived simply."""
    rows: list[Candle] = []
    day = datetime(2026, 6, 15, start[0], start[1], tzinfo=IST)
    prev = base
    for i, close in enumerate(prices):
        c = Decimal(str(close))
        o = prev
        h = max(o, c) + Decimal("10")
        l = min(o, c) - Decimal("10")
        rows.append(Candle(ts=day + timedelta(minutes=step_min * i), open=o, high=h, low=l, close=c, volume=1000))
        prev = c
    return rows


def _strong_trend_up_session() -> list[Candle]:
    # 78 five-minute candles 09:15->15:30; steadily rising, closes near highs.
    prices = [50000 + i * 8 for i in range(78)]
    rows = _session(prices)
    # force last candle close near the day high (strong close location)
    last = rows[-1]
    rows[-1] = Candle(ts=last.ts, open=last.open, high=last.close + Decimal("2"), low=last.open - Decimal("5"),
                      close=last.close + Decimal("1"), volume=1000)
    return rows


def _chop_session() -> list[Candle]:
    # oscillate around 50000 with many vwap crosses, tiny net move.
    prices = []
    for i in range(78):
        prices.append(50000 + (60 if i % 2 == 0 else -60))
    prices[-1] = 50005
    return _session(prices)


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def test_close_location_extremes() -> None:
    assert close_location(Decimal("110"), Decimal("100"), Decimal("110")) == Decimal("1.0000")
    assert close_location(Decimal("110"), Decimal("100"), Decimal("100")) == Decimal("0.0000")
    assert close_location(Decimal("100"), Decimal("100"), Decimal("100")) is None


def test_cumulative_vwap_falls_back_to_unweighted_when_no_volume() -> None:
    rows = [
        _candle(datetime(2026, 6, 15, 9, 15, tzinfo=IST), 100, 102, 98, 100, v=0),
        _candle(datetime(2026, 6, 15, 9, 20, tzinfo=IST), 100, 106, 100, 104, v=0),
    ]
    vwap = cumulative_vwap_series(rows)
    # first vwap == typical price of first candle = (102+98+100)/3 = 100
    assert vwap[0] == Decimal("100")
    assert len(vwap) == 2


def test_vwap_cross_count_counts_sign_changes() -> None:
    rows = _chop_session()
    vwap = cumulative_vwap_series(rows)
    crosses = vwap_cross_count(rows, vwap, Decimal("0.0"))
    assert crosses >= 4


def test_realized_vol_is_non_negative_decimal() -> None:
    rows = _strong_trend_up_session()
    rv = realized_vol_5m(rows)
    assert rv is not None and rv >= 0


# --------------------------------------------------------------------------- #
# feature builder
# --------------------------------------------------------------------------- #
def test_build_day_features_basic_metrics() -> None:
    rows = _strong_trend_up_session()
    feats = build_day_features(session_date="2026-06-15", candles=rows, config=CONFIG, prev_close=Decimal("49950"))
    assert feats.candle_count == 78
    assert feats.day_return_pct is not None and feats.day_return_pct > 0
    assert feats.gap_pct is not None
    assert feats.close_location is not None and feats.close_location > Decimal("0.7")
    assert feats.orb_high is not None and feats.orb_low is not None
    assert len(feats.segments) == 3
    # option chain absent -> warned, not failed
    assert feats.option_chain_available is False
    assert any("option-chain" in w for w in feats.warnings)


def test_missing_option_chain_does_not_fail_and_warns() -> None:
    rows = _strong_trend_up_session()
    feats = build_day_features(session_date="2026-06-15", candles=rows, config=CONFIG)
    assert isinstance(feats, BankNiftyDayFeatures)
    assert feats.atm_iv is None and feats.pcr is None
    assert feats.option_chain_available is False


def test_option_chain_context_is_parsed_when_present() -> None:
    rows = _strong_trend_up_session()
    feats = build_day_features(
        session_date="2026-06-15", candles=rows, config=CONFIG,
        option_chain={"atm_iv": "14.5", "iv_regime": "normal", "pcr": "0.92",
                      "max_pain_strike": "50000", "spot": "50600"},
    )
    assert feats.option_chain_available is True
    assert feats.atm_iv == Decimal("14.5")
    assert feats.pcr == Decimal("0.92")
    assert feats.max_pain_distance_pct is not None


def test_breadth_uses_weights_and_finds_top_contributors() -> None:
    rows = _strong_trend_up_session()
    day = datetime(2026, 6, 15, 9, 15, tzinfo=IST)

    def up_stock(base):
        return [_candle(day + timedelta(minutes=5 * i), base, base + 5, base - 2, base + i, v=500) for i in range(78)]

    constituents = {"NSE:HDFCBANK-EQ": up_stock(1000), "NSE:ICICIBANK-EQ": up_stock(900)}
    weights = {"NSE:HDFCBANK-EQ": Decimal("17.9"), "NSE:ICICIBANK-EQ": Decimal("13.6")}
    feats = build_day_features(session_date="2026-06-15", candles=rows, config=CONFIG,
                               constituent_candles=constituents, weights=weights)
    assert feats.weighted_positive_breadth_pct == Decimal("100.00")
    assert feats.top_positive_contributors


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_classify_strong_trend_day() -> None:
    rows = _strong_trend_up_session()
    feats = build_day_features(session_date="2026-06-15", candles=rows, config=CONFIG, prev_close=Decimal("49950"))
    label = classify_day_rules(feats, CONFIG)
    assert label.primary_class == "trend"
    assert label.direction == "bullish"
    assert Decimal("0") < label.confidence <= Decimal("1")
    assert label.rule_version == CONFIG["rule_version"]


def test_classify_chop_day() -> None:
    rows = _chop_session()
    feats = build_day_features(session_date="2026-06-15", candles=rows, config=CONFIG, prev_close=Decimal("50000"))
    label = classify_day_rules(feats, CONFIG)
    assert label.primary_class in ("chop", "range")


def test_every_sufficient_day_gets_exactly_one_primary_class() -> None:
    for builder in (_strong_trend_up_session, _chop_session):
        feats = build_day_features(session_date="2026-06-15", candles=builder(), config=CONFIG)
        label = classify_day_rules(feats, CONFIG)
        assert label.primary_class in (
            "trend", "range", "spike_channel", "trending_range", "reversal", "chop")


def test_insufficient_candles_still_returns_one_class() -> None:
    rows = _session([50000, 50010, 50020])
    feats = build_day_features(session_date="2026-06-15", candles=rows, config=CONFIG)
    label = classify_day_rules(feats, CONFIG)
    assert label.primary_class == "chop"
    assert "insufficient_data" in label.secondary_tags


# --------------------------------------------------------------------------- #
# nearest neighbour
# --------------------------------------------------------------------------- #
def test_feature_vector_length_and_weighting() -> None:
    feats = build_day_features(session_date="2026-06-15", candles=_strong_trend_up_session(), config=CONFIG)
    vec = feature_vector(feats, CONFIG)
    assert len(vec) == 10
    assert all(isinstance(v, Decimal) for v in vec)


def test_find_nearest_similar_days_ranks_by_distance() -> None:
    target = build_day_features(session_date="2026-06-15", candles=_strong_trend_up_session(), config=CONFIG)
    twin = build_day_features(session_date="2026-06-10", candles=_strong_trend_up_session(), config=CONFIG)
    far = build_day_features(session_date="2026-06-11", candles=_chop_session(), config=CONFIG)
    library = [
        (twin, classify_day_rules(twin, CONFIG)),
        (far, classify_day_rules(far, CONFIG)),
    ]
    similar = find_nearest_similar_days(target, library, CONFIG, top_k=2)
    assert similar[0].session_date == "2026-06-10"  # the twin is nearest
    assert similar[0].distance <= similar[1].distance
    # target itself never appears
    assert all(s.session_date != "2026-06-15" for s in similar)


# --------------------------------------------------------------------------- #
# playbook + exit-model invariant
# --------------------------------------------------------------------------- #
def test_playbook_mentions_runner_exit_and_never_fixed_cap() -> None:
    feats = build_day_features(session_date="2026-06-15", candles=_strong_trend_up_session(), config=CONFIG)
    label = classify_day_rules(feats, CONFIG)
    pb = summarize_playbook(feats, label, CONFIG)
    blob = json.dumps(pb).lower()
    assert "0.5r" in blob and "breakeven" in blob
    assert "mfe" in blob and ("ratchet" in blob or "trail" in blob)
    assert "500" not in blob  # no fixed ₹500 profit cap language
    assert pb["paper_only"] is True


def test_config_enforces_runner_exit_model() -> None:
    exit_model = CONFIG["exit_model"]
    assert exit_model["fixed_target_exit_enabled"] is False
    assert exit_model["profit_lock_trigger"] == 0
    assert exit_model["profit_lock_step"] == 0
    assert Decimal(str(exit_model["breakeven_at_r"])) == Decimal("0.5")
    assert CONFIG["paper_only"] is True
    assert CONFIG["live_orders_enabled"] is False
