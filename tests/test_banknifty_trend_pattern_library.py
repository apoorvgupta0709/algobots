from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.banknifty_trend_patterns import Candle
from scripts.build_banknifty_trend_pattern_library import (
    analyze_sessions,
    classification_record,
    effective_fetch_start,
    feature_record,
    group_by_ist_day,
    history_lookback_calendar_days,
    load_pattern_config,
    parse_args,
    resolve_range,
)

IST = ZoneInfo("Asia/Kolkata")
CONFIG = load_pattern_config(PROJECT_ROOT / "config" / "banknifty_trend_patterns.json")


def _trend_day(day: datetime, base=50000, slope=8) -> list[Candle]:
    rows = []
    prev = Decimal(str(base))
    for i in range(78):
        c = Decimal(str(base + i * slope))
        o = prev
        rows.append(Candle(ts=day + timedelta(minutes=5 * i), open=o, high=max(o, c) + Decimal("10"),
                           low=min(o, c) - Decimal("10"), close=c, volume=1000))
        prev = c
    return rows


def _chop_day(day: datetime, base=50000) -> list[Candle]:
    rows = []
    prev = Decimal(str(base))
    for i in range(78):
        c = Decimal(str(base + (60 if i % 2 == 0 else -60)))
        o = prev
        rows.append(Candle(ts=day + timedelta(minutes=5 * i), open=o, high=max(o, c) + Decimal("5"),
                           low=min(o, c) - Decimal("5"), close=c, volume=1000))
        prev = c
    return rows


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_load_pattern_config_merges_constituent_weights() -> None:
    weights = CONFIG["_constituent_weights"]
    assert weights, "expected constituent weights merged from paper config"
    assert "NSE:HDFCBANK-EQ" in weights
    assert all(isinstance(w, Decimal) for w in weights.values())


def test_load_pattern_config_rejects_unsafe(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"paper_only": True, "live_orders_enabled": True}))
    with pytest.raises(ValueError):
        load_pattern_config(bad)


def _safe_exit_model(**overrides) -> dict:
    em = {
        "fixed_target_exit_enabled": False,
        "profit_lock_trigger": 0,
        "profit_lock_step": 0,
        "breakeven_at_r": 0.5,
        "ratchet_start_r": 1.0,
        "ratchet_giveback_pct": 30,
        "ratchet_giveback_min_inr": 300,
    }
    em.update(overrides)
    return em


def _safe_cfg(**overrides) -> dict:
    cfg = {
        "paper_only": True,
        "live_orders_enabled": False,
        "exit_model": _safe_exit_model(),
    }
    cfg.update(overrides)
    return cfg


def test_load_pattern_config_rejects_string_boolean_paper_only(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(paper_only="true")))  # truthy string, not bool
    with pytest.raises(ValueError):
        load_pattern_config(bad)


def test_load_pattern_config_rejects_string_boolean_live_orders(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(live_orders_enabled="false")))  # truthy string
    with pytest.raises(ValueError):
        load_pattern_config(bad)


def test_load_pattern_config_rejects_fixed_target_exit(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(
        exit_model={"fixed_target_exit_enabled": True, "breakeven_at_r": 0.5})))
    with pytest.raises(ValueError):
        load_pattern_config(bad)


def test_load_pattern_config_rejects_missing_breakeven(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(
        exit_model={"fixed_target_exit_enabled": False})))  # no positive breakeven_at_r
    with pytest.raises(ValueError):
        load_pattern_config(bad)


def test_load_pattern_config_accepts_full_runner_exit_model(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_safe_cfg()))
    cfg = load_pattern_config(good)  # must not raise
    assert cfg["exit_model"]["breakeven_at_r"] == 0.5


@pytest.mark.parametrize(
    "exit_overrides",
    [
        {"breakeven_at_r": 0.8},                       # non-0.5 breakeven
        {"breakeven_at_r": 1},                         # non-0.5 breakeven (int)
        {"breakeven_at_r": "0.5"},                     # string, not a number
        {"breakeven_at_r": True},                      # bool masquerading as number
        {"profit_lock_trigger": 1000},                 # profit-lock re-enabled (nonzero)
        {"profit_lock_step": 500},                     # profit-lock re-enabled (nonzero)
        {"profit_lock_trigger": True},                 # bool, not 0
        {"fixed_target_exit_enabled": True},           # fixed cap re-enabled
        {"fixed_target_exit_enabled": "false"},        # string, not boolean False
        {"ratchet_start_r": 0},                        # not positive
        {"ratchet_start_r": -1.0},                     # negative
        {"ratchet_start_r": "1.0"},                    # string, not a number
        {"ratchet_giveback_pct": 0},                   # not positive
        {"ratchet_giveback_pct": True},                # bool, not a number
        {"ratchet_giveback_min_inr": -1},              # negative floor
    ],
)
def test_load_pattern_config_rejects_unsafe_exit_values(tmp_path: Path, exit_overrides) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(exit_model=_safe_exit_model(**exit_overrides))))
    with pytest.raises(ValueError):
        load_pattern_config(bad)


@pytest.mark.parametrize(
    "drop_key",
    ["profit_lock_trigger", "profit_lock_step", "ratchet_start_r",
     "ratchet_giveback_pct", "ratchet_giveback_min_inr"],
)
def test_load_pattern_config_rejects_missing_exit_keys(tmp_path: Path, drop_key) -> None:
    em = _safe_exit_model()
    em.pop(drop_key)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(exit_model=em)))
    with pytest.raises(ValueError):
        load_pattern_config(bad)


# --------------------------------------------------------------------------- #
# history window + persisted-library similar days (daily --date path)
# --------------------------------------------------------------------------- #
def test_history_lookback_extends_fetch_window_before_start() -> None:
    from datetime import date

    assert history_lookback_calendar_days(CONFIG) > 0
    start = date(2026, 6, 16)
    fetch_start = effective_fetch_start(start, CONFIG)
    # a single-date run must pull prior sessions so gap / ADR / similars can compute
    assert fetch_start < start
    assert (start - fetch_start).days == history_lookback_calendar_days(CONFIG)


def test_analyze_sessions_uses_prior_library_for_similar_days() -> None:
    # Persisted library = two earlier sessions; the target session is analysed on
    # its own (as in a single-date daily run) and must still find similar days.
    older = [
        *_trend_day(datetime(2026, 6, 10, 9, 15, tzinfo=IST)),
        *_chop_day(datetime(2026, 6, 11, 9, 15, tzinfo=IST)),
    ]
    prior_library = analyze_sessions(config=CONFIG, index_by_day=group_by_ist_day(older))

    target_day = datetime(2026, 6, 15, 9, 15, tzinfo=IST)
    target_built = analyze_sessions(
        config=CONFIG,
        index_by_day=group_by_ist_day(_trend_day(target_day)),
        prior_library=prior_library,
    )
    feats, label = target_built[0]
    # without the persisted library this single-date run would have no similars
    assert label.similar_days, "expected similar days drawn from the persisted library"
    assert all(s["session_date"] < feats.session_date for s in label.similar_days)


def test_analyze_sessions_prior_library_does_not_duplicate_in_window_days() -> None:
    # A library row that overlaps an in-window date must not produce a duplicate
    # similar-day entry (in-window build wins).
    days = [
        *_trend_day(datetime(2026, 6, 11, 9, 15, tzinfo=IST)),
        *_trend_day(datetime(2026, 6, 15, 9, 15, tzinfo=IST)),
    ]
    in_window = group_by_ist_day(days)
    overlap_library = analyze_sessions(
        config=CONFIG,
        index_by_day=group_by_ist_day(_trend_day(datetime(2026, 6, 11, 9, 15, tzinfo=IST))),
    )
    built = analyze_sessions(config=CONFIG, index_by_day=in_window, prior_library=overlap_library)
    last = {f.session_date: lbl for f, lbl in built}["2026-06-15"]
    dates = [s["session_date"] for s in last.similar_days]
    assert len(dates) == len(set(dates)), "similar days must not contain duplicates"


# --------------------------------------------------------------------------- #
# grouping
# --------------------------------------------------------------------------- #
def test_group_by_ist_day_splits_sessions() -> None:
    d1 = datetime(2026, 6, 15, 9, 15, tzinfo=IST)
    d2 = datetime(2026, 6, 16, 9, 15, tzinfo=IST)
    grouped = group_by_ist_day([*_trend_day(d1), *_trend_day(d2)])
    assert set(grouped) == {d1.date(), d2.date()}
    assert len(grouped[d1.date()]) == 78


# --------------------------------------------------------------------------- #
# analysis
# --------------------------------------------------------------------------- #
def test_analyze_sessions_labels_every_day_and_attaches_only_past_similars() -> None:
    days = {
        datetime(2026, 6, 11, 9, 15, tzinfo=IST): _trend_day,
        datetime(2026, 6, 12, 9, 15, tzinfo=IST): _chop_day,
        datetime(2026, 6, 15, 9, 15, tzinfo=IST): _trend_day,
    }
    all_candles = []
    for day, builder in days.items():
        all_candles.extend(builder(day))
    index_by_day = group_by_ist_day(all_candles)

    built = analyze_sessions(config=CONFIG, index_by_day=index_by_day)
    assert len(built) == 3
    # every session has exactly one primary class
    assert all(lbl.primary_class for _, lbl in built)

    by_date = {f.session_date: lbl for f, lbl in built}
    # the first (earliest) day can have no similar history
    assert by_date["2026-06-11"].similar_days == []
    # the last day's similars are strictly earlier dates (no look-ahead)
    last_similars = by_date["2026-06-15"].similar_days
    assert last_similars, "expected similar history for the last session"
    assert all(s["session_date"] < "2026-06-15" for s in last_similars)


def test_analyze_sessions_trend_day_is_trend_with_breadth() -> None:
    day = datetime(2026, 6, 15, 9, 15, tzinfo=IST)
    index_by_day = group_by_ist_day(_trend_day(day))

    def up_stock(base):
        return [Candle(ts=day + timedelta(minutes=5 * i), open=Decimal(str(base)),
                       high=Decimal(str(base + 5)), low=Decimal(str(base - 2)),
                       close=Decimal(str(base + i)), volume=500) for i in range(78)]

    constituent_by_day = {
        "NSE:HDFCBANK-EQ": group_by_ist_day(up_stock(1000)),
        "NSE:ICICIBANK-EQ": group_by_ist_day(up_stock(900)),
    }
    weights = {"NSE:HDFCBANK-EQ": Decimal("17.9"), "NSE:ICICIBANK-EQ": Decimal("13.6")}
    built = analyze_sessions(config=CONFIG, index_by_day=index_by_day,
                             constituent_by_day=constituent_by_day, weights=weights)
    feats, label = built[0]
    assert label.primary_class == "trend"
    assert feats.weighted_positive_breadth_pct == Decimal("100.00")


# --------------------------------------------------------------------------- #
# DB record shaping (pure)
# --------------------------------------------------------------------------- #
def test_feature_and_classification_records_are_json_safe() -> None:
    day = datetime(2026, 6, 15, 9, 15, tzinfo=IST)
    built = analyze_sessions(config=CONFIG, index_by_day=group_by_ist_day(_trend_day(day)))
    feats, label = built[0]
    frec = feature_record(feats)
    crec = classification_record(label)
    # jsonb columns must be serialised strings
    assert isinstance(frec["segments"], str) and json.loads(frec["segments"])
    assert isinstance(frec["features"], str)
    assert isinstance(crec["explanation"], str)
    assert isinstance(crec["secondary_tags"], list)
    assert frec["session_date"] == "2026-06-15"
    assert crec["primary_class"] in (
        "trend", "range", "spike_channel", "trending_range", "reversal", "chop")


# --------------------------------------------------------------------------- #
# CLI arg handling
# --------------------------------------------------------------------------- #
def test_resolve_range_single_date() -> None:
    args = parse_args(["--date", "2026-06-16"])
    start, end = resolve_range(args)
    assert start == end and start.isoformat() == "2026-06-16"


def test_resolve_range_requires_bounds() -> None:
    args = parse_args(["--from", "2026-06-01"])
    with pytest.raises(SystemExit):
        resolve_range(args)


def test_parse_args_defaults_and_flags() -> None:
    args = parse_args(["--from", "2025-06-01", "--to", "2026-06-16", "--resolution", "5", "--dry-run", "--print"])
    assert args.start == "2025-06-01" and args.end == "2026-06-16"
    assert args.resolution == "5" and args.dry_run is True and args.do_print is True
