"""Indian cost stack (FY 2026-27 indicative — compendium §1.2) + slippage model.

Every fill in backtest, paper and live P&L accounting flows through CostModel so
the promotion gate compares like with like. Rates are per side unless noted.
"""
from __future__ import annotations

from dataclasses import dataclass

from algobot.core.enums import ProductType, Side

COST_MODEL_VERSION = "fy2026"


def _segment(symbol: str, product: ProductType) -> str:
    """Classify a Fyers symbol into a cost segment."""
    s = symbol.upper()
    if s.endswith(("CE", "PE")) and not s.endswith("-EQ"):
        return "index_option" if any(ix in s for ix in ("NIFTY", "SENSEX", "BANKEX")) \
            else "stock_option"
    if "FUT" in s:
        return "index_future" if any(ix in s for ix in ("NIFTY", "SENSEX", "BANKEX")) \
            else "stock_future"
    if product == ProductType.CNC:
        return "delivery"
    return "intraday_equity"


@dataclass(frozen=True)
class SegmentRates:
    brokerage_flat: float        # per order, capped
    brokerage_pct: float         # % of turnover (min vs flat applies; 0.0 => flat only)
    stt_buy_pct: float           # % of turnover on buy
    stt_sell_pct: float          # % of turnover on sell (options: % of premium)
    exchange_pct: float          # exchange transaction charge, % of turnover
    stamp_buy_pct: float         # stamp duty, buy side only
    sebi_per_crore: float = 10.0
    gst_pct: float = 18.0        # on brokerage + exchange txn + sebi


RATES: dict[str, SegmentRates] = {
    "delivery":        SegmentRates(0.0, 0.0,      0.1,   0.1,    0.00297, 0.015),
    "intraday_equity": SegmentRates(20.0, 0.03,    0.0,   0.025,  0.00297, 0.003),
    "index_future":    SegmentRates(20.0, 0.03,    0.0,   0.02,   0.00173, 0.002),
    "stock_future":    SegmentRates(20.0, 0.03,    0.0,   0.02,   0.00210, 0.002),
    # brokerage_pct 0.0 => flat per-order brokerage only (options are flat Rs 20)
    # Options STT (sell side, % of premium) is 0.10% since 2024-10-01
    # (Finance (No. 2) Act 2024); 0.15% here previously overstated the largest
    # per-trade cost line by 50%.
    "index_option":    SegmentRates(20.0, 0.0,     0.0,   0.10,   0.03503, 0.003),
    "stock_option":    SegmentRates(20.0, 0.0,     0.0,   0.10,   0.05030, 0.003),
}

# Slippage assumptions (compendium §8.1): fraction of traded price, per side.
SLIPPAGE_PCT: dict[str, float] = {
    "index_option": 0.20,     # ~0.2% of premium
    "stock_option": 1.20,     # ~1.2% of premium
    "index_future": 0.01,
    "stock_future": 0.03,
    "intraday_equity": 0.03,
    "delivery": 0.05,
}


class CostModel:
    """Order-level transaction costs and slippage."""

    version = COST_MODEL_VERSION

    def order_costs(self, symbol: str, side: Side, qty: int, price: float,
                    product: ProductType = ProductType.INTRADAY) -> float:
        """Total charges for one order (one side of a round trip), in rupees."""
        seg = _segment(symbol, product)
        r = RATES[seg]
        turnover = abs(qty) * price

        if r.brokerage_flat and r.brokerage_pct:
            brokerage = min(r.brokerage_flat, r.brokerage_pct / 100 * turnover)
        else:
            brokerage = r.brokerage_flat
        stt = (r.stt_buy_pct if side == Side.BUY else r.stt_sell_pct) / 100 * turnover
        exchange = r.exchange_pct / 100 * turnover
        stamp = r.stamp_buy_pct / 100 * turnover if side == Side.BUY else 0.0
        sebi = r.sebi_per_crore * turnover / 1e7
        gst = r.gst_pct / 100 * (brokerage + exchange + sebi)
        return round(brokerage + stt + exchange + stamp + sebi + gst, 2)

    def round_trip_costs(self, symbol: str, qty: int, entry_price: float,
                         exit_price: float,
                         product: ProductType = ProductType.INTRADAY) -> float:
        return (self.order_costs(symbol, Side.BUY, qty, entry_price, product)
                + self.order_costs(symbol, Side.SELL, qty, exit_price, product))

    def slippage_pct(self, symbol: str,
                     product: ProductType = ProductType.INTRADAY) -> float:
        """Per-side slippage as % of price."""
        return SLIPPAGE_PCT[_segment(symbol, product)]

    def apply_slippage(self, symbol: str, side: Side, price: float,
                       product: ProductType = ProductType.INTRADAY) -> float:
        """Worsen a fill price by the segment's slippage (buy up, sell down)."""
        slip = self.slippage_pct(symbol, product) / 100 * price
        return round(price + slip if side == Side.BUY else price - slip, 2)
