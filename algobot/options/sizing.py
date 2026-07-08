"""Shared structure sizing for backtest AND live/paper order building.

Historically only the backtest sized option structures off margin/risk;
the live/paper path traded whatever ``leg.lots`` the strategy factory
hardcoded, so a strategy's paper track record was earned at a size its
live deployment would never use. Both paths now share this helper.
"""
from __future__ import annotations

import logging
import math

from algobot.core.models import OptionStructure

log = logging.getLogger(__name__)


def structure_lots(structure: OptionStructure, spot: float, lot: int,
                   risk_amt: float, capital: float) -> int:
    """Lots for credit/undefined-risk structures via the margin subsystem.

    Returns 0 when one lot's estimated margin exceeds capital (do not trade),
    and falls back to 1 lot when ``algobot.options.margin`` is unavailable.
    """
    try:
        from algobot.options.margin import estimate_margin  # lazy
        margin = float(estimate_margin(structure, spot, lot))
        if margin <= 0 or not math.isfinite(margin):
            return 1
        if margin > capital:
            log.warning("Margin/lot %.0f exceeds capital %.0f for %s — skipping",
                        margin, capital, structure.name)
            return 0
        return max(int(risk_amt // margin), 1)
    except ImportError:
        return 1
    except Exception:
        log.debug("estimate_margin failed for %s — 1 lot", structure.name,
                  exc_info=True)
        return 1
