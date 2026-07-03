"""Named instrument universes.

Strategy metas may list a universe KEY instead of concrete symbols; the runner
resolves it here. Keys keep strategy files small and let the universe be
updated in one place. Symbols use Fyers notation.
"""
from __future__ import annotations

# Index spot symbols (data/signals) and their derivative roots (execution).
NIFTY = "NSE:NIFTY50-INDEX"
BANKNIFTY = "NSE:NIFTYBANK-INDEX"
FINNIFTY = "NSE:FINNIFTY-INDEX"
INDIAVIX = "NSE:INDIAVIX-INDEX"

INDEX_LOT_SIZES = {  # January-2026 cycle; reconfirm on nseindia.com before sizing
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
    "SENSEX": 20,
}

STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50}

# Liquid large-cap equities (Nifty-50 heavyweights) — swing/intraday equity universe.
NIFTY50_CORE: list[str] = [
    "NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:INFY-EQ",
    "NSE:TCS-EQ", "NSE:SBIN-EQ", "NSE:AXISBANK-EQ", "NSE:KOTAKBANK-EQ",
    "NSE:LT-EQ", "NSE:ITC-EQ", "NSE:BHARTIARTL-EQ", "NSE:BAJFINANCE-EQ",
    "NSE:MARUTI-EQ", "NSE:M&M-EQ", "NSE:TITAN-EQ", "NSE:SUNPHARMA-EQ",
    "NSE:TATAMOTORS-EQ", "NSE:TATASTEEL-EQ", "NSE:HINDUNILVR-EQ", "NSE:NTPC-EQ",
]

# Cointegration candidates for pair trading (same-sector liquid F&O names).
PAIR_CANDIDATES: list[tuple[str, str]] = [
    ("NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ"),
    ("NSE:INFY-EQ", "NSE:TCS-EQ"),
    ("NSE:AXISBANK-EQ", "NSE:KOTAKBANK-EQ"),
    ("NSE:SBIN-EQ", "NSE:BANKBARODA-EQ"),
]

# ETFs for long-term core / asset-allocation strategies (delivery).
ETF_UNIVERSE = {
    "equity_core": "NSE:NIFTYBEES-EQ",
    "equity_next50": "NSE:JUNIORBEES-EQ",
    "debt": "NSE:LIQUIDBEES-EQ",
    "gold": "NSE:GOLDBEES-EQ",
}

# Sector indices for rotation strategies (spot indices; trade via sector ETFs/leaders).
SECTOR_INDICES: dict[str, str] = {
    "BANK": "NSE:NIFTYBANK-INDEX",
    "IT": "NSE:NIFTYIT-INDEX",
    "AUTO": "NSE:NIFTYAUTO-INDEX",
    "PHARMA": "NSE:NIFTYPHARMA-INDEX",
    "FMCG": "NSE:NIFTYFMCG-INDEX",
    "METAL": "NSE:NIFTYMETAL-INDEX",
    "ENERGY": "NSE:NIFTYENERGY-INDEX",
    "REALTY": "NSE:NIFTYREALTY-INDEX",
}

_UNIVERSES: dict[str, list[str]] = {
    "NIFTY_INDEX": [NIFTY],
    "BANKNIFTY_INDEX": [BANKNIFTY],
    "INDEX_UNIVERSE": [NIFTY, BANKNIFTY],
    "NIFTY50_UNIVERSE": NIFTY50_CORE,
    "ETF_UNIVERSE": list(ETF_UNIVERSE.values()),
    "SECTOR_UNIVERSE": list(SECTOR_INDICES.values()),
    "PAIR_UNIVERSE": sorted({s for pair in PAIR_CANDIDATES for s in pair}),
}


def resolve(instruments: list[str]) -> list[str]:
    """Expand universe keys; pass concrete symbols through."""
    out: list[str] = []
    for item in instruments:
        out.extend(_UNIVERSES.get(item, [item]))
    # de-dupe, preserve order
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def lot_size(underlying: str) -> int:
    """Lot size for an index underlying symbol or root.

    Longest root matched first so 'BANKNIFTY' is not shadowed by 'NIFTY'.
    """
    up = underlying.upper().replace("NIFTYBANK", "BANKNIFTY")
    for root in sorted(INDEX_LOT_SIZES, key=len, reverse=True):
        if root in up:
            return INDEX_LOT_SIZES[root]
    return 1


def strike_step(underlying: str) -> int:
    up = underlying.upper().replace("NIFTYBANK", "BANKNIFTY")
    for root in sorted(STRIKE_STEPS, key=len, reverse=True):
        if root in up:
            return STRIKE_STEPS[root]
    return 50
