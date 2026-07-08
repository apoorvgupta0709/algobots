"""Tests for the 2026-07-08 algobot realism fixes: options STT rate, and the
volatility-smile consistency between strike selection and backtest pricing.
"""
from __future__ import annotations

import pandas as pd
import pytest

from algobot.backtest.option_data import OptionDataProvider
from algobot.core.enums import ProductType, Side
from algobot.costs.india import RATES, CostModel

# Fyers weekly format: NSE:NIFTY<YY><Mcode><DD><strike><CE|PE>. 26=year, 7=July,
# 31=day, then strike. ATM and an OTM call, same expiry (a month out so both
# carry real premium at the valuation time).
ATM_SYMBOL = "NSE:NIFTY2673124500CE"
OTM_SYMBOL = "NSE:NIFTY2673125000CE"
TS = pd.Timestamp("2026-07-01 10:00:00", tz="Asia/Kolkata")
SPOT = 24500.0


def test_index_option_sell_stt_rate_is_ten_bps():
    # STT on option sell is 0.10% of premium since 2024-10-01 (was 0.15%).
    assert RATES["index_option"].stt_sell_pct == 0.10
    assert RATES["stock_option"].stt_sell_pct == 0.10


def test_index_option_sell_costs_reflect_ten_bps_stt():
    # qty 100 @ premium 200 -> turnover 20000. Exact cost stack at 0.10% STT:
    # brokerage 20 + STT 20 + exchange 7.006 + sebi 0.02 + gst 4.86468 = 51.89.
    # At the old 0.15% STT it would have been 61.89, a 10-rupee difference.
    cm = CostModel()
    costs = cm.order_costs(ATM_SYMBOL, Side.SELL, 100, 200.0, ProductType.MARGIN)
    assert costs == pytest.approx(51.89, abs=0.01)


def test_synthetic_pricing_applies_smile_to_otm_leg(monkeypatch):
    provider = OptionDataProvider(iv_source=0.14)
    atm = provider._synthetic(ATM_SYMBOL, TS, SPOT)
    otm_with_smile = provider._synthetic(OTM_SYMBOL, TS, SPOT)
    assert atm > 0 and otm_with_smile > 0

    # With the smile flattened to zero the OTM leg prices strictly lower — the
    # fix lifts exactly the wings that strike selection already priced up.
    import algobot.options.chain as chain_mod

    monkeypatch.setattr(chain_mod, "SMILE_SLOPE", 0.0)
    otm_flat = provider._synthetic(OTM_SYMBOL, TS, SPOT)
    assert otm_with_smile > otm_flat


def test_atm_pricing_unaffected_by_smile(monkeypatch):
    # At the money |K/S - 1| == 0, so the smile multiplier is 1.0 either way.
    provider = OptionDataProvider(iv_source=0.14)
    with_smile = provider._synthetic(ATM_SYMBOL, TS, SPOT)

    import algobot.options.chain as chain_mod

    monkeypatch.setattr(chain_mod, "SMILE_SLOPE", 0.0)
    flat = provider._synthetic(ATM_SYMBOL, TS, SPOT)
    assert with_smile == flat
