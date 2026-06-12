from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.option_chain_signals import (  # noqa: E402
    ChainRow,
    classify_iv_regime,
    compute_atm_iv,
    compute_max_pain,
    compute_oi_buildup,
    compute_pcr,
    nearest_strike,
    total_oi,
)


def row(strike, option_type, *, oi=None, iv=None, oi_change=None) -> ChainRow:
    return ChainRow(
        underlying="BANKNIFTY",
        expiry=None,
        strike=Decimal(str(strike)),
        option_type=option_type,
        oi=oi,
        iv=None if iv is None else Decimal(str(iv)),
        oi_change=oi_change,
    )


def sample_chain() -> list[ChainRow]:
    # Spot ~ 51000. PE OI heavier than CE OI -> PCR > 1.
    return [
        row(50800, "CE", oi=1000, iv=18.0),
        row(50800, "PE", oi=4000, iv=20.0),
        row(50900, "CE", oi=1500, iv=17.0),
        row(50900, "PE", oi=3000, iv=19.0),
        row(51000, "CE", oi=2000, iv=16.0),
        row(51000, "PE", oi=2500, iv=18.0),
        row(51100, "CE", oi=3000, iv=15.5),
        row(51100, "PE", oi=1200, iv=17.5),
        row(51200, "CE", oi=4000, iv=15.0),
        row(51200, "PE", oi=900, iv=17.0),
    ]


def test_total_oi_sums_each_side():
    rows = sample_chain()
    assert total_oi(rows, "CE") == 1000 + 1500 + 2000 + 3000 + 4000
    assert total_oi(rows, "PE") == 4000 + 3000 + 2500 + 1200 + 900


def test_pcr_is_pe_over_ce_oi():
    rows = sample_chain()
    expected = Decimal(total_oi(rows, "PE")) / Decimal(total_oi(rows, "CE"))
    pcr = compute_pcr(rows)
    assert pcr == expected.quantize(Decimal("0.000001"))
    assert pcr is not None and pcr > 1  # PE-heavy book


def test_pcr_none_when_no_ce_oi():
    rows = [row(100, "PE", oi=10)]
    assert compute_pcr(rows) is None


def test_nearest_strike_picks_atm():
    assert nearest_strike(sample_chain(), Decimal("51030")) == Decimal("51000")


def test_atm_iv_averages_ce_pe_at_atm():
    rows = sample_chain()
    atm_iv = compute_atm_iv(rows, Decimal("51010"))
    # ATM strike 51000: CE iv 16.0, PE iv 18.0 -> mean 17.0
    assert atm_iv == Decimal("17.000000")


def test_atm_iv_none_without_spot_or_iv():
    assert compute_atm_iv(sample_chain(), None) is None
    no_iv = [row(100, "CE", oi=1), row(100, "PE", oi=1)]
    assert compute_atm_iv(no_iv, Decimal("100")) is None


def test_max_pain_minimizes_writer_payout():
    # Construct a book where CE OI piles below and PE OI piles above 51000 so the
    # intrinsic payout is minimized at the middle strike.
    rows = [
        row(50800, "CE", oi=100), row(50800, "PE", oi=100),
        row(51000, "CE", oi=100), row(51000, "PE", oi=100),
        row(51200, "CE", oi=100), row(51200, "PE", oi=100),
    ]
    # Symmetric book -> max pain at the central strike.
    assert compute_max_pain(rows) == Decimal("51000")


def test_max_pain_none_without_oi():
    rows = [row(100, "CE"), row(100, "PE")]
    assert compute_max_pain(rows) is None


def test_iv_regime_buckets_against_history():
    history = [Decimal(str(v)) for v in (10, 11, 12, 13, 14, 20, 21, 22, 23, 24)]
    assert classify_iv_regime(Decimal("10.5"), history) == "low"
    assert classify_iv_regime(Decimal("17"), history) == "normal"
    assert classify_iv_regime(Decimal("23.5"), history) == "high"


def test_iv_regime_unknown_without_history():
    assert classify_iv_regime(Decimal("17"), None) == "unknown"
    assert classify_iv_regime(None, [Decimal("1")]) == "unknown"


def test_oi_buildup_uses_explicit_change():
    rows = [
        row(100, "CE", oi=1000, oi_change=200),
        row(100, "PE", oi=1000, oi_change=900),
    ]
    out = compute_oi_buildup(rows)
    assert out["ce_oi_change"] == 200
    assert out["pe_oi_change"] == 900
    assert out["label"] == "put_buildup"


def test_oi_buildup_falls_back_to_prev_snapshot():
    prev = [row(100, "CE", oi=1000), row(100, "PE", oi=1000)]
    curr = [row(100, "CE", oi=1500), row(100, "PE", oi=1100)]
    out = compute_oi_buildup(curr, prev)
    assert out["ce_oi_change"] == 500
    assert out["pe_oi_change"] == 100
    assert out["label"] == "call_buildup"


def test_oi_buildup_flat_when_no_change():
    rows = [row(100, "CE", oi=1000), row(100, "PE", oi=1000)]
    assert compute_oi_buildup(rows)["label"] == "flat"
