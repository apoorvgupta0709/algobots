#!/usr/bin/env python3
"""Pure, deterministic option-chain math shared by the ingester, engine, and dashboard.

No network / DB / LLM calls. All money/greek math is Decimal-based so the
ingester's stored summary, the engine's entry gate, and the dashboard's display
agree on the exact same numbers. Every function tolerates missing fields and
returns None rather than raising on incomplete chains.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

SIX_PLACES = Decimal("0.000001")
TWO_PLACES = Decimal("0.01")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except Exception:
        return None
    return dec if dec.is_finite() else None


def _to_int(value: Any) -> int | None:
    dec = _to_decimal(value)
    return None if dec is None else int(dec)


@dataclass(frozen=True)
class ChainRow:
    """One strike/option-type leg of an option chain snapshot.

    A normalized, source-agnostic view: the FYERS-specific field-name juggling
    lives in the ingester, everything downstream consumes this dataclass.
    """

    underlying: str
    expiry: Any  # date | None
    strike: Decimal
    option_type: str  # 'CE' or 'PE'
    symbol: str | None = None
    ltp: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None
    oi: int | None = None
    oi_change: int | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    iv: Decimal | None = None


def compute_pcr(rows: Iterable[ChainRow]) -> Decimal | None:
    """Put-Call Ratio by open interest: total PE OI / total CE OI.

    >1 is typically read as bearish/oversold positioning, <1 as bullish.
    Returns None when CE OI is zero/absent (ratio undefined)."""
    ce_oi = sum(r.oi for r in rows if r.option_type == "CE" and r.oi is not None)
    pe_oi = sum(r.oi for r in rows if r.option_type == "PE" and r.oi is not None)
    if not ce_oi:
        return None
    return (Decimal(pe_oi) / Decimal(ce_oi)).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def total_oi(rows: Iterable[ChainRow], option_type: str) -> int:
    """Summed open interest for one side; missing OI counts as zero."""
    return sum(r.oi for r in rows if r.option_type == option_type and r.oi is not None)


def compute_max_pain(rows: Iterable[ChainRow]) -> Decimal | None:
    """Max-pain strike: the expiry price that minimizes total intrinsic payout to
    option holders (i.e. maximizes writer profit), weighted by open interest.

    For each candidate expiry price P (every listed strike), pain(P) =
      sum_CE oi_ce * max(0, P - strike_ce) + sum_PE oi_pe * max(0, strike_pe - P).
    The argmin is the max-pain strike. Returns None when there is no OI to weight."""
    rows = list(rows)
    strikes = sorted({r.strike for r in rows})
    if not strikes:
        return None
    has_oi = any(r.oi for r in rows if r.oi is not None)
    if not has_oi:
        return None

    best_strike: Decimal | None = None
    best_pain: Decimal | None = None
    for price in strikes:
        pain = Decimal("0")
        for r in rows:
            if r.oi is None or r.oi <= 0:
                continue
            if r.option_type == "CE" and price > r.strike:
                pain += Decimal(r.oi) * (price - r.strike)
            elif r.option_type == "PE" and price < r.strike:
                pain += Decimal(r.oi) * (r.strike - price)
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_strike = price
    return best_strike


def nearest_strike(rows: Iterable[ChainRow], spot: Decimal) -> Decimal | None:
    """The listed strike closest to spot (the ATM strike)."""
    strikes = {r.strike for r in rows}
    if not strikes:
        return None
    return min(strikes, key=lambda s: (abs(s - spot), s))


def compute_atm_iv(rows: Iterable[ChainRow], spot: Decimal | None) -> Decimal | None:
    """Average of CE and PE implied volatility at the strike nearest spot.

    Falls back to whichever side has IV when only one is present; None when the
    chain carries no IV (e.g. greeks not requested)."""
    rows = list(rows)
    if spot is None:
        return None
    atm = nearest_strike(rows, spot)
    if atm is None:
        return None
    ivs = [r.iv for r in rows if r.strike == atm and r.iv is not None]
    if not ivs:
        return None
    return (sum(ivs, Decimal("0")) / Decimal(len(ivs))).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def classify_iv_regime(
    atm_iv: Decimal | None,
    history: Iterable[Decimal] | None = None,
    *,
    low_pct: Decimal = Decimal("33"),
    high_pct: Decimal = Decimal("67"),
) -> str:
    """Bucket ATM IV against its own recent history into low/normal/high.

    Long options prefer 'low' (cheap vol). With no history we cannot rank, so we
    return 'unknown' rather than guess. Percentile is the fraction of historical
    observations at or below the current value, scaled to 0-100."""
    if atm_iv is None:
        return "unknown"
    hist = [h for h in (history or []) if h is not None]
    if not hist:
        return "unknown"
    at_or_below = sum(1 for h in hist if h <= atm_iv)
    percentile = Decimal(at_or_below) / Decimal(len(hist)) * Decimal("100")
    if percentile <= low_pct:
        return "low"
    if percentile >= high_pct:
        return "high"
    return "normal"


def compute_oi_buildup(
    curr_rows: Iterable[ChainRow],
    prev_rows: Iterable[ChainRow] | None = None,
) -> dict[str, Any]:
    """Net OI change per side between two snapshots.

    Prefers each row's own oi_change (exchange-reported) and falls back to the
    delta vs the previous snapshot when oi_change is absent. Returns per-side
    totals plus a coarse label: rising CE OI relative to PE suggests call writing
    (bearish pressure) and vice versa."""
    curr = list(curr_rows)
    prev_by_key = {}
    for r in prev_rows or []:
        prev_by_key[(r.strike, r.option_type)] = r

    ce_change = 0
    pe_change = 0
    for r in curr:
        change = r.oi_change
        if change is None and prev_by_key:
            prev = prev_by_key.get((r.strike, r.option_type))
            if prev is not None and prev.oi is not None and r.oi is not None:
                change = r.oi - prev.oi
        if change is None:
            continue
        if r.option_type == "CE":
            ce_change += change
        elif r.option_type == "PE":
            pe_change += change

    if ce_change == 0 and pe_change == 0:
        label = "flat"
    elif pe_change > ce_change:
        label = "put_buildup"   # rising PE OI relative to CE — bullish lean
    else:
        label = "call_buildup"  # rising CE OI relative to PE — bearish lean
    return {"ce_oi_change": ce_change, "pe_oi_change": pe_change, "label": label}
