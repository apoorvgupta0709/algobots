"""LegBuilder: turn StrikeRule/ExpiryRule legs into concrete tradable symbols.

The same resolution code runs in backtest, paper and live: strategies emit
OptionStructures full of rules; the builder snapshots them against spot/now.

The data-layer helpers (``algobot.data.expiries.next_expiry`` and
``algobot.data.instruments.option_symbol/future_symbol/root_of``) are imported
LAZILY inside :meth:`LegBuilder.resolve` — that module is under parallel
construction and importing it at module load would couple the packages.
"""
from __future__ import annotations

import copy
import datetime as dt
from typing import Callable, Optional

from algobot.core.enums import OptionType
from algobot.core.models import OptionLeg, OptionStructure
from algobot.core.universes import strike_step
from algobot.options.chain import OptionChain


class LegBuilder:
    """Resolves OptionStructure legs into symbols/strikes/expiries.

    Args:
        chain_provider: optional callable ``underlying -> OptionChain`` used
            for "delta" / "premium_pct" strike rules (e.g. a live quote chain).
            When None a synthetic BS chain is built from spot/now.
    """

    def __init__(self, chain_provider: Optional[Callable[[str], OptionChain]] = None) -> None:
        self.chain_provider = chain_provider

    def resolve(self, structure: OptionStructure, spot: float,
                now: dt.datetime) -> OptionStructure:
        """Return a DEEP COPY of ``structure`` with every leg's
        ``resolved_symbol`` / ``resolved_strike`` / ``resolved_expiry`` filled.

        Strike resolution by ``StrikeRule.method``:

        - ``atm``: ATM strike + value * strike_step.
        - ``delta`` / ``premium_pct``: looked up on the option chain
          (synthetic BS chain when no ``chain_provider`` was given).
        - ``absolute``: the value itself.
        - ``pct_otm``: spot * (1 + pct/100) for CE, spot * (1 - pct/100) for
          PE, rounded to the strike step.
        - ``rel``: the previously resolved same-option-type leg's strike +
          value * strike_step (condor/fly wings: width stays fixed wherever
          the delta-resolved shorts land).

        FUT legs get a symbol/expiry only.
        """
        from algobot.data.expiries import next_expiry
        from algobot.data.instruments import future_symbol, option_symbol, root_of

        resolved = copy.deepcopy(structure)
        root = root_of(resolved.underlying)
        step = float(strike_step(resolved.underlying))
        on_date = now.date() if isinstance(now, dt.datetime) else now

        chain: Optional[OptionChain] = None  # built lazily, shared across legs

        def get_chain() -> OptionChain:
            nonlocal chain
            if chain is None:
                if self.chain_provider is not None:
                    chain = self.chain_provider(resolved.underlying)
                else:
                    chain = OptionChain.synthetic(resolved.underlying, spot, now)
            return chain

        last_strike_by_type: dict[OptionType, float] = {}

        for leg in resolved.legs:
            expiry = next_expiry(root, leg.expiry_rule.kind, leg.expiry_rule.n, on_date)
            leg.resolved_expiry = expiry.isoformat()

            if leg.option_type == OptionType.FUT:
                leg.resolved_symbol = future_symbol(root, expiry)
                continue

            strike = self._resolve_strike(leg, spot, step, expiry,
                                          last_strike_by_type, get_chain)
            leg.resolved_strike = float(strike)
            last_strike_by_type[leg.option_type] = float(strike)
            leg.resolved_symbol = option_symbol(root, expiry, strike,
                                                leg.option_type.value)
        return resolved

    @staticmethod
    def _resolve_strike(leg: OptionLeg, spot: float, step: float, expiry: dt.date,
                        last_strike_by_type: dict[OptionType, float],
                        get_chain: Callable[[], OptionChain]) -> float:
        rule = leg.strike_rule
        opt_type = leg.option_type.value
        atm = round(spot / step) * step

        if rule.method == "atm":
            return atm + rule.value * step
        if rule.method == "absolute":
            return float(rule.value)
        if rule.method == "pct_otm":
            raw = spot * (1 + rule.value / 100.0) if opt_type == "CE" \
                else spot * (1 - rule.value / 100.0)
            return round(raw / step) * step
        if rule.method == "delta":
            return get_chain().strike_by_delta(rule.value, opt_type, expiry)
        if rule.method == "premium_pct":
            return get_chain().strike_by_premium_pct(rule.value, opt_type, expiry)
        if rule.method == "rel":
            base = last_strike_by_type.get(leg.option_type)
            if base is None:
                raise ValueError(
                    "StrikeRule 'rel' needs a previously resolved leg of the "
                    f"same option type ({opt_type}) in the structure"
                )
            return base + rule.value * step
        raise ValueError(f"Unknown StrikeRule method: {rule.method!r}")

    def resolve_many(self, structures: list[OptionStructure], spot: float,
                     now: dt.datetime) -> list[OptionStructure]:
        """Convenience: resolve several structures against the same snapshot."""
        return [self.resolve(s, spot, now) for s in structures]
