"""Shared enumerations for the entire platform.

Values chosen to map directly onto Fyers API conventions where relevant
(OrderType/Side integer codes match fyers_apiv3 place_order payloads).
"""
from enum import Enum, IntEnum


class Category(str, Enum):
    LONGTERM = "longterm"
    SWING = "swing"
    INTRADAY = "intraday"
    OPTIONS = "options"
    FUTURES = "futures"


class Timeframe(str, Enum):
    """Primary decision timeframe. Values are Fyers `resolution` strings."""
    MIN5 = "5"
    MIN15 = "15"
    HOUR1 = "60"
    DAY = "D"


class Mode(str, Enum):
    """Lifecycle mode of a strategy. Promotion order: OFF -> BACKTEST -> PAPER -> LIVE."""
    OFF = "off"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class SignalType(str, Enum):
    ENTRY_LONG = "entry_long"
    ENTRY_SHORT = "entry_short"
    EXIT = "exit"
    ADJUST = "adjust"          # modify/replace legs of an open structure
    REBALANCE = "rebalance"    # portfolio strategies: move to target weights


class Side(IntEnum):
    BUY = 1
    SELL = -1


class OrderType(IntEnum):
    LIMIT = 1
    MARKET = 2
    STOP = 3        # SL-M
    STOP_LIMIT = 4  # SL-L


class ProductType(str, Enum):
    CNC = "CNC"            # delivery
    INTRADAY = "INTRADAY"  # MIS, auto square-off by broker
    MARGIN = "MARGIN"      # NRML carry-forward for F&O


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"
    FUT = "FUT"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PLACED = "placed"
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ExitReason(str, Enum):
    STOP_LOSS = "sl"
    TAKE_PROFIT = "tp"
    TRAIL = "trail"
    SQUAREOFF = "squareoff"   # intraday end-of-day flatten
    SIGNAL = "signal"         # strategy emitted EXIT
    KILL = "kill"             # kill switch / risk cap
    EXPIRY = "expiry"         # option/future settled at expiry
    TIME = "time"             # time stop
