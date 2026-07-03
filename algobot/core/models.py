"""Core dataclasses: the vocabulary shared by strategies, backtester, paper and live execution.

Strategies emit `Signal`s only. The OrderManager converts signals into `Order`s
(sizing from stop distance, risk caps, kill switch) and routes them to a broker.
Option strategies attach an `OptionStructure` whose legs carry *rules*
(StrikeRule/ExpiryRule) that the LegBuilder resolves into concrete symbols at
execution time — the same resolution code runs in backtest, paper and live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from algobot.core.enums import (
    ExitReason,
    Mode,
    OptionType,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    SignalType,
)


# --------------------------------------------------------------------------- options
@dataclass(frozen=True)
class StrikeRule:
    """How to pick a strike. Exactly one of the class-method constructors."""
    method: str                 # "atm" | "delta" | "premium_pct" | "absolute" | "pct_otm" | "rel"
    value: float = 0.0

    @classmethod
    def atm(cls, offset: int = 0) -> "StrikeRule":
        """ATM +/- N strike steps (offset in steps, e.g. +2 = 2 strikes OTM for a call)."""
        return cls("atm", offset)

    @classmethod
    def delta(cls, target: float) -> "StrikeRule":
        """Nearest strike to |delta| target, e.g. 0.30."""
        return cls("delta", target)

    @classmethod
    def premium_pct(cls, pct_of_spot: float) -> "StrikeRule":
        """Strike whose premium is closest to pct_of_spot % of the underlying."""
        return cls("premium_pct", pct_of_spot)

    @classmethod
    def absolute(cls, strike: float) -> "StrikeRule":
        return cls("absolute", strike)

    @classmethod
    def pct_otm(cls, pct: float) -> "StrikeRule":
        """Strike ~pct % away from spot on the OTM side (sign-aware via option type)."""
        return cls("pct_otm", pct)


@dataclass(frozen=True)
class ExpiryRule:
    """Which expiry to trade. n=0 is the nearest, n=1 the next one, ..."""
    kind: str                   # "weekly" | "monthly"
    n: int = 0

    @classmethod
    def weekly(cls, n: int = 0) -> "ExpiryRule":
        return cls("weekly", n)

    @classmethod
    def monthly(cls, n: int = 0) -> "ExpiryRule":
        return cls("monthly", n)


@dataclass
class OptionLeg:
    side: Side
    option_type: OptionType
    strike_rule: StrikeRule
    expiry_rule: ExpiryRule
    lots: int = 1
    # Filled in by the LegBuilder at execution time:
    resolved_symbol: Optional[str] = None
    resolved_strike: Optional[float] = None
    resolved_expiry: Optional[str] = None   # ISO date

    def to_dict(self) -> dict:
        return {
            "side": int(self.side),
            "option_type": self.option_type.value,
            "strike_rule": {"method": self.strike_rule.method, "value": self.strike_rule.value},
            "expiry_rule": {"kind": self.expiry_rule.kind, "n": self.expiry_rule.n},
            "lots": self.lots,
            "resolved_symbol": self.resolved_symbol,
            "resolved_strike": self.resolved_strike,
            "resolved_expiry": self.resolved_expiry,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OptionLeg":
        return cls(
            side=Side(d["side"]),
            option_type=OptionType(d["option_type"]),
            strike_rule=StrikeRule(d["strike_rule"]["method"], d["strike_rule"]["value"]),
            expiry_rule=ExpiryRule(d["expiry_rule"]["kind"], d["expiry_rule"]["n"]),
            lots=d.get("lots", 1),
            resolved_symbol=d.get("resolved_symbol"),
            resolved_strike=d.get("resolved_strike"),
            resolved_expiry=d.get("resolved_expiry"),
        )


@dataclass
class OptionStructure:
    """A named multi-leg position on a single underlying (straddle, condor, ...)."""
    name: str                   # "iron_condor", "short_straddle", "bull_call_spread", ...
    underlying: str             # e.g. "NSE:NIFTY50-INDEX"
    legs: list[OptionLeg]
    net_direction: str = "debit"  # "debit" | "credit" — drives margin & risk model

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "underlying": self.underlying,
            "net_direction": self.net_direction,
            "legs": [leg.to_dict() for leg in self.legs],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "OptionStructure":
        return cls(
            name=d["name"],
            underlying=d["underlying"],
            legs=[OptionLeg.from_dict(x) for x in d["legs"]],
            net_direction=d.get("net_direction", "debit"),
        )


# --------------------------------------------------------------------------- signals
@dataclass
class PairLeg:
    """Second leg of a pair/spread trade: opposite side, hedge-ratio sized."""
    symbol: str
    hedge_ratio: float = 1.0    # qty2 = round(qty1 * hedge_ratio)


@dataclass
class SizeHint:
    """Portfolio strategies (SIP, rebalance) express size directly instead of risk-sizing."""
    weight: Optional[float] = None      # fraction of allocated capital
    notional: Optional[float] = None    # absolute rupees
    qty: Optional[int] = None           # exact quantity/lots


@dataclass
class Signal:
    strategy_id: str
    signal_type: SignalType
    instrument: str                     # underlying / equity symbol
    timestamp: datetime
    reference_price: float              # underlying price signal was computed on
    stop_loss: Optional[float] = None   # on the underlying
    take_profit: Optional[float] = None
    structure: Optional[OptionStructure] = None
    pair_leg: Optional[PairLeg] = None
    size_hint: Optional[SizeHint] = None
    product_type: ProductType = ProductType.INTRADAY
    validity: Optional[timedelta] = None  # None = this bar only (runner default)
    reason: str = ""                    # human-readable trigger note (journal)
    tags: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- orders / fills
@dataclass
class Order:
    strategy_id: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    product_type: ProductType = ProductType.INTRADAY
    mode: Mode = Mode.PAPER
    tag: str = ""
    signal_id: Optional[int] = None
    broker_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    id: Optional[int] = None


@dataclass
class Fill:
    order_id: int
    symbol: str
    side: Side
    qty: int
    price: float
    timestamp: datetime


@dataclass
class Position:
    strategy_id: str
    symbol: str
    qty: int                            # signed: >0 long, <0 short
    avg_price: float
    mode: Mode
    opened_at: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    underlying: Optional[str] = None    # for option legs: index the SL/TP refers to
    underlying_entry: Optional[float] = None
    structure_id: Optional[str] = None  # groups legs of one OptionStructure
    trail_anchor: Optional[float] = None  # MFE ratchet: best underlying price seen
    product_type: ProductType = ProductType.INTRADAY
    id: Optional[int] = None

    @property
    def is_long(self) -> bool:
        return self.qty > 0


@dataclass
class Trade:
    """A closed round trip (possibly multi-leg, aggregated by structure_id)."""
    strategy_id: str
    mode: Mode
    symbol: str
    direction: str                      # "long" | "short"
    qty: int
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: ExitReason
    structure_json: Optional[dict] = None
    modeled_exit_price: Optional[float] = None  # stop-fire fidelity: price the model wanted
    id: Optional[int] = None
