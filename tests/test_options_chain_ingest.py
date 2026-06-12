from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.ingest_fyers_optionchain import (  # noqa: E402
    build_summary,
    normalize_chain_response,
    underlying_label,
)

EXPIRY_EPOCH = 1749744000  # fixed epoch -> deterministic expiry date


def sample_response() -> dict:
    expiry_date = datetime.fromtimestamp(EXPIRY_EPOCH, tz=timezone.utc).date()
    return {
        "s": "ok",
        "code": 200,
        "data": {
            "expiryData": [{"date": expiry_date.strftime("%d-%m-%Y"), "expiry": str(EXPIRY_EPOCH)}],
            "optionsChain": [
                # Index/underlying row carries spot; strike -1, blank option_type.
                {"symbol": "NSE:NIFTYBANK-INDEX", "strike_price": -1, "option_type": "", "ltp": 51000.0, "fp": 51000.0},
                {"symbol": "NSE:BANKNIFTY50900CE", "strike_price": 50900, "option_type": "CE",
                 "ltp": 220.0, "bid": 219.0, "ask": 221.0, "volume": 1200, "oi": 12000, "oich": 300,
                 "delta": 0.62, "gamma": 0.0012, "theta": -14.0, "vega": 9.0, "iv": 17.5},
                {"symbol": "NSE:BANKNIFTY50900PE", "strike_price": 50900, "option_type": "PE",
                 "ltp": 95.0, "bid": 94.0, "ask": 96.0, "volume": 800, "oi": 4000, "oich": -100,
                 "delta": -0.38, "gamma": 0.0011, "theta": -12.0, "vega": 8.0, "iv": 19.0},
                {"symbol": "NSE:BANKNIFTY51000CE", "strike_price": 51000, "option_type": "CE",
                 "ltp": 160.0, "bid": 159.0, "ask": 161.0, "volume": 2000, "oi": 15000, "oich": 500,
                 "delta": 0.51, "gamma": 0.0013, "theta": -15.0, "vega": 10.0, "iv": 16.0},
                {"symbol": "NSE:BANKNIFTY51000PE", "strike_price": 51000, "option_type": "PE",
                 "ltp": 150.0, "bid": 149.0, "ask": 151.0, "volume": 1900, "oi": 9000, "oich": 200,
                 "delta": -0.49, "gamma": 0.0013, "theta": -15.0, "vega": 10.0, "iv": 18.0},
            ],
        },
    }


def test_underlying_label_maps_known_and_unknown():
    assert underlying_label("NSE:NIFTYBANK-INDEX") == "BANKNIFTY"
    assert underlying_label("NSE:NIFTY50-INDEX") == "NIFTY"
    assert underlying_label("NSE:FINNIFTY-INDEX") == "FINNIFTY"


def test_normalize_skips_index_row_and_captures_spot():
    rows, spot, expiry = normalize_chain_response("NSE:NIFTYBANK-INDEX", sample_response())
    assert spot == Decimal("51000.0")
    assert len(rows) == 4  # index row excluded
    assert all(r.option_type in {"CE", "PE"} for r, _ in rows)
    assert expiry == datetime.fromtimestamp(EXPIRY_EPOCH, tz=timezone.utc).date()


def test_normalize_maps_fields_and_greeks():
    rows, _, _ = normalize_chain_response("NSE:NIFTYBANK-INDEX", sample_response())
    ce_atm = next(r for r, _ in rows if r.strike == Decimal("51000") and r.option_type == "CE")
    assert ce_atm.underlying == "BANKNIFTY"
    assert ce_atm.ltp == Decimal("160.0")
    assert ce_atm.bid == Decimal("159.0")
    assert ce_atm.ask == Decimal("161.0")
    assert ce_atm.volume == 2000
    assert ce_atm.oi == 15000
    assert ce_atm.oi_change == 500
    assert ce_atm.delta == Decimal("0.51")
    assert ce_atm.iv == Decimal("16.0")
    assert ce_atm.symbol == "NSE:BANKNIFTY51000CE"


def test_normalize_handles_alternate_field_names():
    response = {
        "data": {
            "optionsChain": [
                {"strike": 100, "optionType": "CE", "openInterest": 500, "implied_volatility": 12.0, "vol": 5},
            ]
        }
    }
    rows, _, _ = normalize_chain_response("NSE:NIFTY50-INDEX", response)
    assert len(rows) == 1
    r, _ = rows[0]
    assert r.oi == 500
    assert r.iv == Decimal("12.0")
    assert r.volume == 5
    assert r.option_type == "CE"


def test_build_summary_aggregates_chain():
    rows, spot, expiry = normalize_chain_response("NSE:NIFTYBANK-INDEX", sample_response())
    summary = build_summary("NSE:NIFTYBANK-INDEX", [r for r, _ in rows], spot, expiry)
    assert summary["underlying"] == "BANKNIFTY"
    assert summary["spot"] == Decimal("51000.0")
    assert summary["atm_strike"] == Decimal("51000")
    assert summary["total_ce_oi"] == 12000 + 15000
    assert summary["total_pe_oi"] == 4000 + 9000
    # PCR = 13000 / 27000 < 1 (CE-heavy)
    assert summary["pcr"] is not None and summary["pcr"] < 1
    # ATM 51000: CE iv 16, PE iv 18 -> mean 17
    assert summary["atm_iv"] == Decimal("17.000000")
    assert summary["iv_regime"] == "unknown"  # no history supplied


def test_build_summary_uses_iv_history_for_regime():
    rows, spot, expiry = normalize_chain_response("NSE:NIFTYBANK-INDEX", sample_response())
    history = [Decimal(str(v)) for v in (5, 6, 7, 8, 9)]  # current ATM IV 17 >> history -> high
    summary = build_summary("NSE:NIFTYBANK-INDEX", [r for r, _ in rows], spot, expiry, history)
    assert summary["iv_regime"] == "high"
