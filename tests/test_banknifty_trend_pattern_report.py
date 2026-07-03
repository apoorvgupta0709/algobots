from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.generate_banknifty_trend_pattern_report import (
    classification_from_row,
    features_from_row,
    load_report_config,
    parse_args,
    render_report,
)

CONFIG = json.loads((PROJECT_ROOT / "config" / "banknifty_trend_patterns.json").read_text())


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


# --------------------------------------------------------------------------- #
# report config safety validation
# --------------------------------------------------------------------------- #
def test_load_report_config_accepts_repo_config() -> None:
    cfg = load_report_config(PROJECT_ROOT / "config" / "banknifty_trend_patterns.json")
    assert cfg["paper_only"] is True and cfg["live_orders_enabled"] is False


def test_load_report_config_rejects_string_boolean_paper_only(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(paper_only="true")))
    with pytest.raises(ValueError):
        load_report_config(bad)


def test_load_report_config_rejects_unsafe_exit_model(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(
        exit_model={"fixed_target_exit_enabled": True, "breakeven_at_r": 0.5})))
    with pytest.raises(ValueError):
        load_report_config(bad)


def test_load_report_config_rejects_live_orders_enabled(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(live_orders_enabled=True)))
    with pytest.raises(ValueError):
        load_report_config(bad)


def test_load_report_config_accepts_full_runner_exit_model(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_safe_cfg()))
    cfg = load_report_config(good)  # must not raise
    assert cfg["exit_model"]["breakeven_at_r"] == 0.5


@pytest.mark.parametrize(
    "exit_overrides",
    [
        {"breakeven_at_r": 0.8},                 # non-0.5 breakeven
        {"breakeven_at_r": "0.5"},               # string, not a number
        {"breakeven_at_r": True},                # bool masquerading as number
        {"breakeven_at_r": False},               # bool masquerading as number
        {"profit_lock_trigger": 1000},           # profit-lock re-enabled (nonzero)
        {"profit_lock_trigger": "0"},            # string, not a number
        {"profit_lock_trigger": True},           # bool masquerading as number
        {"profit_lock_step": 500},               # profit-lock re-enabled (nonzero)
        {"profit_lock_step": "0"},               # string, not a number
        {"profit_lock_step": False},             # bool masquerading as number
        {"fixed_target_exit_enabled": True},     # fixed cap re-enabled
        {"ratchet_start_r": 0},                  # not positive
        {"ratchet_start_r": "1.0"},              # string, not a number
        {"ratchet_start_r": True},               # bool masquerading as number
        {"ratchet_giveback_pct": -5},            # negative
        {"ratchet_giveback_pct": "30"},          # string, not a number
        {"ratchet_giveback_pct": True},          # bool masquerading as number
        {"ratchet_giveback_min_inr": -1},        # negative floor
        {"ratchet_giveback_min_inr": "300"},     # string, not a number
        {"ratchet_giveback_min_inr": False},     # bool masquerading as number
    ],
)
def test_load_report_config_rejects_unsafe_exit_values(tmp_path: Path, exit_overrides) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(exit_model=_safe_exit_model(**exit_overrides))))
    with pytest.raises(ValueError):
        load_report_config(bad)


@pytest.mark.parametrize(
    "drop_key",
    ["breakeven_at_r", "profit_lock_trigger", "profit_lock_step", "ratchet_start_r",
     "ratchet_giveback_pct", "ratchet_giveback_min_inr"],
)
def test_load_report_config_rejects_missing_exit_keys(tmp_path: Path, drop_key) -> None:
    em = _safe_exit_model()
    em.pop(drop_key)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_safe_cfg(exit_model=em)))
    with pytest.raises(ValueError):
        load_report_config(bad)


def _feature_row() -> dict:
    return {
        "session_date": "2026-06-15",
        "underlying": "BANKNIFTY",
        "underlying_symbol": "NSE:NIFTYBANK-INDEX",
        "resolution": "5",
        "open": "50000", "high": "50620", "low": "49980", "close": "50610", "prev_close": "49950",
        "gap_pct": "0.10", "day_return_pct": "1.22", "day_range_pct": "1.28",
        "orb_high": "50080", "orb_low": "49990", "orb_range_pct": "0.18",
        "orb_break_direction": "up", "orb_hold": True,
        "close_location": "0.9800", "vwap_cross_count": 2, "vwap_side_pct": "92.00",
        "realized_vol": "0.1200", "range_vs_adr10": "1.1000",
        "mfe_from_open_pct": "1.24", "mae_from_open_pct": "-0.04",
        "day_high_time": "15:20", "day_low_time": "09:20",
        "weighted_positive_breadth_pct": "100.00", "weighted_negative_breadth_pct": "0.00",
        "weighted_vwap_confirm_pct": "100.00", "breadth_divergence": False,
        "top_positive_contributors": [{"symbol": "NSE:HDFCBANK-EQ", "contribution": 1.2, "move_pct": 0.8}],
        "top_negative_contributors": [],
        "atm_iv": None, "iv_regime": None, "pcr": None, "max_pain_distance_pct": None,
        "option_chain_available": False, "candle_count": 78,
        "segments": [
            {"name": "open_drive", "start_ist": "09:15", "end_ist": "10:15", "return_pct": 0.3,
             "range_pct": 0.4, "vwap_side_pct": 90.0, "net_direction": "up", "volume_share": 25.0,
             "close_location": 0.8, "candle_count": 12},
        ],
        "features": {}, "warnings": ["option-chain context unavailable for session"],
    }


def _classification_row() -> dict:
    return {
        "session_date": "2026-06-15",
        "classification_id": 42,
        "primary_class": "trend",
        "direction": "bullish",
        "confidence": "0.7200",
        "rule_version": "banknifty_trend_patterns_v1",
        "algorithm": "deterministic_rules",
        "secondary_tags": ["spike_channel"],
        "explanation": {"scores": {"trend": 3.1}},
        "similar_days": [
            {"session_date": "2026-06-09", "primary_class": "trend", "direction": "bullish",
             "similarity": 0.91, "distance": 0.1, "note": "trend/bullish, ret 1.81%"},
        ],
    }


def test_render_report_has_all_sections() -> None:
    feats = features_from_row(_feature_row())
    label = classification_from_row(_classification_row())
    md, payload = render_report(feats, label, CONFIG)

    assert "## 1. Classification" in md
    assert "## 2. Evidence" in md
    assert "## 3. Similar historical days" in md
    assert "## 4. How it could have been played" in md
    assert "## 5. Bot lessons" in md
    assert "`trend`" in md
    assert "2026-06-09" in md  # similar day rendered
    assert payload["classification"]["primary_class"] == "trend"
    assert payload["paper_only"] is True


def test_report_exit_model_is_runner_style_not_fixed_cap() -> None:
    feats = features_from_row(_feature_row())
    label = classification_from_row(_classification_row())
    md, payload = render_report(feats, label, CONFIG)

    low = md.lower()
    # how-it-could-have-been-played + bot lessons mention 0.5R breakeven + MFE trail/ratchet
    assert "0.5r" in low and "breakeven" in low
    assert "mfe" in low and ("ratchet" in low or "trail" in low)
    # never a fixed ₹500 profit cap (the phrase, not the digits inside prices like 50000)
    assert "₹500" not in md
    assert "500 profit" not in low and "profit cap of" not in low
    assert "fixed profit cap" not in low or "no fixed profit cap" in low
    assert payload["exit_model"]


def test_report_marks_missing_option_chain_without_guessing() -> None:
    feats = features_from_row(_feature_row())
    label = classification_from_row(_classification_row())
    md, _ = render_report(feats, label, CONFIG)
    assert "Option chain:** unavailable" in md
    assert "warned, not guessed" in md


def test_report_states_paper_research_only() -> None:
    feats = features_from_row(_feature_row())
    label = classification_from_row(_classification_row())
    md, _ = render_report(feats, label, CONFIG)
    low = md.lower()
    assert "paper-only" in low or "paper/research" in low
    assert "no live orders" in low


def test_features_and_classification_roundtrip_from_row() -> None:
    feats = features_from_row(_feature_row())
    label = classification_from_row(_classification_row())
    assert feats.session_date == "2026-06-15"
    assert feats.close_location is not None and float(feats.close_location) > 0.9
    assert feats.option_chain_available is False
    assert label.primary_class == "trend"
    assert label.similar_days and label.similar_days[0]["session_date"] == "2026-06-09"


def test_parse_args_requires_date() -> None:
    with pytest.raises(SystemExit):
        parse_args([])
    args = parse_args(["--date", "2026-06-16", "--print"])
    assert args.single == "2026-06-16" and args.do_print is True
