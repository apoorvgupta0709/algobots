"""Rough SPAN-style margin estimation for option structures.

Deliberately conservative rules of thumb (sizing/risk gating, NOT broker
truth):

- naked short option  ~ 13% of underlying notional per lot,
- defined-risk spread ~ spread width * lot per short/long pair,
- iron structures (defined-risk shorts on BOTH sides) ~ max of the two sides
  (exchange charges one side since only one can lose at expiry),
- futures            ~ 13% of notional,
- long options       ~ 0 (premium is paid upfront, not margined).

Works best on structures already resolved by the LegBuilder; unresolved legs
fall back to what the rules imply ("absolute"/"atm"/"pct_otm" strikes are
computed, "rel" longs pair by their fixed width, anything else is treated as
naked).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from algobot.core.enums import OptionType, Side
from algobot.core.models import OptionLeg, OptionStructure
from algobot.core.universes import strike_step

SHORT_MARGIN_PCT = 0.13
FUT_MARGIN_PCT = 0.13


@dataclass
class _Unit:
    """One lot-unit of an option leg for pairing purposes."""
    strike: Optional[float]        # None when it cannot be inferred
    rel_width: Optional[float]     # |rel value| * step, for unresolved wings


def _approx_strike(leg: OptionLeg, spot: float, step: float) -> Optional[float]:
    """Best-effort strike for an unresolved leg (None when rule needs a chain)."""
    if leg.resolved_strike is not None:
        return float(leg.resolved_strike)
    rule = leg.strike_rule
    if rule.method == "absolute":
        return float(rule.value)
    if rule.method == "atm":
        return round(spot / step) * step + rule.value * step
    if rule.method == "pct_otm":
        raw = spot * (1 + rule.value / 100.0) if leg.option_type == OptionType.CE \
            else spot * (1 - rule.value / 100.0)
        return round(raw / step) * step
    return None  # delta / premium_pct / rel need resolution


def _side_margin(legs: list[OptionLeg], spot: float, lot: int,
                 step: float) -> tuple[float, bool, bool]:
    """Margin for all legs of one option type.

    Returns (margin, has_short, fully_hedged).
    """
    shorts: list[_Unit] = []
    longs: list[_Unit] = []
    for leg in legs:
        strike = _approx_strike(leg, spot, step)
        rel_width = abs(leg.strike_rule.value) * step \
            if (leg.resolved_strike is None and leg.strike_rule.method == "rel") else None
        bucket = shorts if leg.side == Side.SELL else longs
        for _ in range(max(leg.lots, 0)):
            bucket.append(_Unit(strike=strike, rel_width=rel_width))

    naked_unit = SHORT_MARGIN_PCT * spot * lot
    margin = 0.0
    fully_hedged = bool(shorts)
    available = list(longs)

    for short in shorts:
        width: Optional[float] = None
        pick: Optional[_Unit] = None
        if short.strike is not None:
            # tightest strike-known long → smallest (broker-favorable) width
            candidates = [u for u in available if u.strike is not None]
            if candidates:
                pick = min(candidates, key=lambda u: abs(u.strike - short.strike))
                width = abs(pick.strike - short.strike)
        if width is None:
            # unresolved wing carrying its own fixed width ("rel" rule)
            candidates = [u for u in available if u.rel_width is not None]
            if candidates:
                pick = candidates[0]
                width = pick.rel_width
        if width is not None and pick is not None:
            margin += width * lot
            available.remove(pick)
        else:
            margin += naked_unit
            fully_hedged = False
    return margin, bool(shorts), fully_hedged


def estimate_margin(structure: OptionStructure, spot: float, lot: int) -> float:
    """Estimated margin (rupees) to carry ``structure`` at ``spot``.

    Args:
        structure: ideally already resolved by the LegBuilder.
        spot: underlying price.
        lot: contract lot size (see :func:`algobot.core.universes.lot_size`).
    """
    step = float(strike_step(structure.underlying))
    fut_margin = 0.0
    by_type: dict[OptionType, list[OptionLeg]] = {OptionType.CE: [], OptionType.PE: []}

    for leg in structure.legs:
        if leg.option_type == OptionType.FUT:
            fut_margin += FUT_MARGIN_PCT * spot * lot * max(leg.lots, 0)
        else:
            by_type[leg.option_type].append(leg)

    ce_m, ce_short, ce_hedged = _side_margin(by_type[OptionType.CE], spot, lot, step)
    pe_m, pe_short, pe_hedged = _side_margin(by_type[OptionType.PE], spot, lot, step)

    if ce_short and pe_short and ce_hedged and pe_hedged:
        # iron condor / iron fly: only one wing can be tested at expiry
        option_margin = max(ce_m, pe_m)
    else:
        option_margin = ce_m + pe_m
    return option_margin + fut_margin
