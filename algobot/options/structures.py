"""Factory functions for standard option structures.

Each factory returns an :class:`~algobot.core.models.OptionStructure` whose
legs carry strike/expiry RULES — the LegBuilder resolves them into concrete
symbols at execution time. ``net_direction`` reflects whether the structure is
opened for a net debit or net credit (drives the margin/risk model).

Leg ordering matters for "rel" wing rules: the short leg of each option type
is listed before its wing so the wing resolves relative to it.
"""
from __future__ import annotations

from algobot.core.enums import OptionType, Side
from algobot.core.models import ExpiryRule, OptionLeg, OptionStructure, StrikeRule


def _leg(side: Side, opt_type: OptionType, strike_rule: StrikeRule,
         expiry_rule: ExpiryRule, lots: int) -> OptionLeg:
    return OptionLeg(side=side, option_type=opt_type, strike_rule=strike_rule,
                     expiry_rule=expiry_rule, lots=lots)


def long_option(underlying: str, opt_type: OptionType, strike_rule: StrikeRule,
                expiry_rule: ExpiryRule = ExpiryRule.weekly(),
                lots: int = 1) -> OptionStructure:
    """Plain long CE/PE."""
    return OptionStructure(
        name=f"long_{opt_type.value.lower()}",
        underlying=underlying,
        legs=[_leg(Side.BUY, opt_type, strike_rule, expiry_rule, lots)],
        net_direction="debit",
    )


def straddle(underlying: str, side: Side,
             expiry_rule: ExpiryRule = ExpiryRule.weekly(),
             lots: int = 1) -> OptionStructure:
    """ATM straddle: CE + PE at the ATM strike, both on ``side``."""
    rule = StrikeRule.atm()
    prefix = "long" if side == Side.BUY else "short"
    return OptionStructure(
        name=f"{prefix}_straddle",
        underlying=underlying,
        legs=[
            _leg(side, OptionType.CE, rule, expiry_rule, lots),
            _leg(side, OptionType.PE, rule, expiry_rule, lots),
        ],
        net_direction="debit" if side == Side.BUY else "credit",
    )


def strangle(underlying: str, side: Side, strike_rule_call: StrikeRule,
             strike_rule_put: StrikeRule,
             expiry_rule: ExpiryRule = ExpiryRule.weekly(),
             lots: int = 1) -> OptionStructure:
    """OTM strangle with independent call/put strike rules."""
    prefix = "long" if side == Side.BUY else "short"
    return OptionStructure(
        name=f"{prefix}_strangle",
        underlying=underlying,
        legs=[
            _leg(side, OptionType.CE, strike_rule_call, expiry_rule, lots),
            _leg(side, OptionType.PE, strike_rule_put, expiry_rule, lots),
        ],
        net_direction="debit" if side == Side.BUY else "credit",
    )


def vertical_spread(underlying: str, opt_type: OptionType, direction: str,
                    buy_rule: StrikeRule, sell_rule: StrikeRule,
                    expiry_rule: ExpiryRule = ExpiryRule.weekly(),
                    lots: int = 1) -> OptionStructure:
    """Two-leg vertical: buy one strike, sell another, same expiry.

    Args:
        direction: ``"debit"`` (e.g. bull call spread) or ``"credit"``
            (e.g. bear call spread); stored as ``net_direction``.
    """
    if direction not in ("debit", "credit"):
        raise ValueError("direction must be 'debit' or 'credit'")
    return OptionStructure(
        name=f"{opt_type.value.lower()}_{direction}_spread",
        underlying=underlying,
        legs=[
            _leg(Side.BUY, opt_type, buy_rule, expiry_rule, lots),
            _leg(Side.SELL, opt_type, sell_rule, expiry_rule, lots),
        ],
        net_direction=direction,
    )


def iron_condor(underlying: str, short_delta: float = 0.20, wing_steps: int = 4,
                expiry_rule: ExpiryRule = ExpiryRule.weekly(),
                lots: int = 1) -> OptionStructure:
    """Delta-selected short strangle + fixed-width wings.

    Shorts are placed at ``|delta| ~ short_delta``; the wings use "rel" rules
    (± ``wing_steps`` strike steps from the resolved short of the same type)
    so the wing width stays fixed wherever the shorts land.
    """
    return OptionStructure(
        name="iron_condor",
        underlying=underlying,
        legs=[
            _leg(Side.SELL, OptionType.CE, StrikeRule.delta(short_delta), expiry_rule, lots),
            _leg(Side.BUY, OptionType.CE, StrikeRule("rel", wing_steps), expiry_rule, lots),
            _leg(Side.SELL, OptionType.PE, StrikeRule.delta(short_delta), expiry_rule, lots),
            _leg(Side.BUY, OptionType.PE, StrikeRule("rel", -wing_steps), expiry_rule, lots),
        ],
        net_direction="credit",
    )


def iron_butterfly(underlying: str, wing_steps: int = 6,
                   expiry_rule: ExpiryRule = ExpiryRule.weekly(),
                   lots: int = 1) -> OptionStructure:
    """Short ATM straddle with protective wings ``wing_steps`` steps away."""
    return OptionStructure(
        name="iron_butterfly",
        underlying=underlying,
        legs=[
            _leg(Side.SELL, OptionType.CE, StrikeRule.atm(), expiry_rule, lots),
            _leg(Side.BUY, OptionType.CE, StrikeRule("rel", wing_steps), expiry_rule, lots),
            _leg(Side.SELL, OptionType.PE, StrikeRule.atm(), expiry_rule, lots),
            _leg(Side.BUY, OptionType.PE, StrikeRule("rel", -wing_steps), expiry_rule, lots),
        ],
        net_direction="credit",
    )


def covered_call(underlying: str, call_rule: StrikeRule,
                 expiry_rule: ExpiryRule = ExpiryRule.weekly(),
                 lots: int = 1) -> OptionStructure:
    """Short call overlay (the stock/future leg is held separately)."""
    return OptionStructure(
        name="covered_call",
        underlying=underlying,
        legs=[_leg(Side.SELL, OptionType.CE, call_rule, expiry_rule, lots)],
        net_direction="credit",
    )


def calendar(underlying: str, opt_type: OptionType, strike_rule: StrikeRule,
             near: ExpiryRule = ExpiryRule.weekly(0),
             far: ExpiryRule = ExpiryRule.monthly(0),
             lots: int = 1) -> OptionStructure:
    """Horizontal calendar: sell the near expiry, buy the far, same strike rule."""
    return OptionStructure(
        name=f"{opt_type.value.lower()}_calendar",
        underlying=underlying,
        legs=[
            _leg(Side.SELL, opt_type, strike_rule, near, lots),
            _leg(Side.BUY, opt_type, strike_rule, far, lots),
        ],
        net_direction="debit",
    )


def diagonal_pmcc(underlying: str, long_delta: float = 0.75,
                  short_delta: float = 0.30, lots: int = 1) -> OptionStructure:
    """Poor-man's covered call: deep-ITM next-monthly long call financed by a
    ~0.30-delta weekly short call."""
    return OptionStructure(
        name="diagonal_pmcc",
        underlying=underlying,
        legs=[
            _leg(Side.BUY, OptionType.CE, StrikeRule.delta(long_delta),
                 ExpiryRule.monthly(1), lots),
            _leg(Side.SELL, OptionType.CE, StrikeRule.delta(short_delta),
                 ExpiryRule.weekly(0), lots),
        ],
        net_direction="debit",
    )


def ratio_backspread(underlying: str, opt_type: OptionType, sell_rule: StrikeRule,
                     buy_rule: StrikeRule,
                     expiry_rule: ExpiryRule = ExpiryRule.weekly(),
                     lots: int = 1) -> OptionStructure:
    """1x2 ratio backspread: sell 1 (nearer the money), buy 2 further out.

    Conventionally opened at/near net credit — the single richer short funds
    the two cheaper longs — so ``net_direction`` is "credit".
    """
    return OptionStructure(
        name=f"{opt_type.value.lower()}_ratio_backspread",
        underlying=underlying,
        legs=[
            _leg(Side.SELL, opt_type, sell_rule, expiry_rule, lots),
            _leg(Side.BUY, opt_type, buy_rule, expiry_rule, 2 * lots),
        ],
        net_direction="credit",
    )
