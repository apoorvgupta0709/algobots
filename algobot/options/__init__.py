"""Options toolkit: BS pricing, chains, leg resolution, structures, margin."""
from algobot.options.chain import OptionChain
from algobot.options.leg_builder import LegBuilder
from algobot.options.margin import estimate_margin
from algobot.options.pricing import (
    bs_greeks,
    bs_price,
    implied_vol,
    synthetic_premium_series,
)
from algobot.options.structures import (
    calendar,
    covered_call,
    diagonal_pmcc,
    iron_butterfly,
    iron_condor,
    long_option,
    ratio_backspread,
    straddle,
    strangle,
    vertical_spread,
)

__all__ = [
    "OptionChain",
    "LegBuilder",
    "estimate_margin",
    "bs_greeks",
    "bs_price",
    "implied_vol",
    "synthetic_premium_series",
    "calendar",
    "covered_call",
    "diagonal_pmcc",
    "iron_butterfly",
    "iron_condor",
    "long_option",
    "ratio_backspread",
    "straddle",
    "strangle",
    "vertical_spread",
]
