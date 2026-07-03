"""Fill pricing and position-sizing helpers for the backtest engine.

Central risk rule (the risk-management OS): every risk trade risks
``capital * risk_per_trade_pct / 100`` from the stop distance.  Derivatives
trade whole lots; defined-risk option structures are sized so the estimated
max loss stays within the risk budget (minimum one lot, logged when even one
lot exceeds it — the structure itself hard-caps the loss).
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from algobot.core.enums import ProductType, Side
from algobot.core.models import OptionStructure, SizeHint
from algobot.costs.india import CostModel

log = logging.getLogger(__name__)


def risk_amount(capital: float, risk_pct: float) -> float:
    """Rupees risked per trade under the central risk rule."""
    return capital * risk_pct / 100.0


def fill_price(cost_model: CostModel, symbol: str, side: Side, raw_price: float,
               product: ProductType) -> float:
    """Raw price worsened by the segment slippage (buy up / sell down)."""
    return cost_model.apply_slippage(symbol, side, raw_price, product)


def order_cost(cost_model: CostModel, symbol: str, side: Side, qty: int,
               price: float, product: ProductType) -> float:
    return cost_model.order_costs(symbol, side, abs(int(qty)), price, product)


# ------------------------------------------------------------------- sizing
def qty_from_hint(hint: SizeHint, price: float, capital: float) -> int:
    """Cash-instrument quantity from an explicit SizeHint."""
    if hint.qty is not None:
        return max(int(hint.qty), 0)
    notional = hint.notional if hint.notional is not None else \
        (hint.weight or 0.0) * capital
    return max(int(notional // price), 0) if price > 0 else 0


def lots_from_hint(hint: SizeHint, per_lot_value: float, capital: float) -> int:
    """Derivative lots from an explicit SizeHint (qty means lots)."""
    if hint.qty is not None:
        return max(int(hint.qty), 0)
    notional = hint.notional if hint.notional is not None else \
        (hint.weight or 0.0) * capital
    if per_lot_value <= 0:
        return 1
    return max(int(notional // per_lot_value), 0)


def cash_qty(risk_amt: float, entry: float, stop: Optional[float],
             capital: float) -> int:
    """Risk-rule quantity for CASH instruments (no lot rounding), notional-capped."""
    if stop is None or entry <= 0:
        log.warning("Cash entry without stop_loss — cannot risk-size, skipping")
        return 0
    dist = abs(entry - stop)
    if dist <= 0:
        log.warning("Zero stop distance at entry %.2f — skipping", entry)
        return 0
    qty = int(risk_amt / dist)
    return max(min(qty, int(capital // entry)), 0)


def derivative_lots(risk_amt: float, entry: float, stop: Optional[float],
                    lot: int) -> int:
    """Risk-rule lots for linear derivatives (futures): whole lots, min 1."""
    if stop is None or abs(entry - stop) <= 0:
        log.warning("Futures entry without a usable stop — defaulting to 1 lot")
        return 1
    qty = risk_amt / abs(entry - stop)
    lots = int(qty // lot)
    if lots < 1:
        log.warning("Risk budget %.0f < one lot (%d) at stop distance %.2f — "
                    "taking minimum 1 lot", risk_amt, lot, abs(entry - stop))
        return 1
    return lots


def debit_lots(risk_amt: float, per_lot_debit: float, capital: float) -> int:
    """Lots for a debit structure: max loss (net debit) within the risk budget."""
    if per_lot_debit <= 0:
        return 1
    lots = int(risk_amt // per_lot_debit)
    if lots < 1:
        log.warning("Debit/lot %.0f exceeds risk budget %.0f — taking minimum "
                    "1 lot (defined-risk structure)", per_lot_debit, risk_amt)
        lots = 1
    return max(min(lots, int(capital // per_lot_debit)), 0) or 0


def credit_lots(structure: OptionStructure, spot: float, lot: int,
                risk_amt: float, capital: float) -> int:
    """Lots for credit/undefined-risk structures via the margin subsystem.

    Falls back to 1 lot when ``algobot.options.margin`` is unavailable.
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
