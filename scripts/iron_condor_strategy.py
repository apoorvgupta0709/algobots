#!/usr/bin/env python3
"""BankNifty Iron Condor — pure signal/risk logic.

Paper-only intraday neutral strategy for BankNifty index options.
Sells an OTM put credit spread + OTM call credit spread to collect
premium in range-bound markets.

Max loss: ₹18,000 per trade (600-pt wing × 30 lot BankNifty)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0


@dataclass(frozen=True)
class IronCondorSignal:
    """Signal returned when a valid iron condor setup is detected."""
    strategy_id: str
    direction: str
    structure: str
    entry_time: datetime
    underlying_entry: Decimal
    sold_put_strike: Decimal
    bought_put_strike: Decimal
    sold_call_strike: Decimal
    bought_call_strike: Decimal
    net_credit: Decimal
    max_loss_rupees: Decimal
    stop_underlying: Decimal
    target_underlying: Decimal
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


def D(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def nearest_strike(price: Decimal, available_strikes: list[Decimal], step: Decimal) -> Decimal:
    """Round price to the nearest available strike."""
    if not available_strikes:
        return (price / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    rounded = (price / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return min(available_strikes, key=lambda s: abs(s - rounded))


def is_range_day(candles: list[Candle], spot: Decimal, range_pct_threshold: Decimal = Decimal("0.010")) -> bool:
    """True if day-to-entry range is tight (<1.0% of spot by default)."""
    if not candles or spot <= 0:
        return False
    day_high = max(c.high for c in candles)
    day_low = min(c.low for c in candles)
    return (day_high - day_low) / spot < range_pct_threshold


def low_body_candle(candle: Candle, spot: Decimal, body_pct: Decimal = Decimal("0.003")) -> bool:
    """True if candle has small body and wicks on both sides (no conviction)."""
    body = abs(candle.close - candle.open)
    if body / spot > body_pct if spot > 0 else False:
        return False
    upper_wick = candle.high - max(candle.open, candle.close)
    lower_wick = min(candle.open, candle.close) - candle.low
    return upper_wick > 0 and lower_wick > 0


def proxy_premium(strike: Decimal, spot: Decimal, opt_type: str, days_to_expiry: int) -> Decimal:
    """Realistic BankNifty option premium proxy.

    BankNifty weekly ATM options: ~1.0-1.2% of spot = ₹580-₹700 at 58k.
    OTM ~0.5% (~₹290 away): trades at ~70-80% of ATM premium = ₹400-₹560.
    OTM ~1.0% (~₹580 away): trades at ~50-65% of ATM premium = ₹290-₹455.
    OTM ~2.0% (~₹1,160 away): trades at ~25-35% of ATM premium = ₹145-₹245.
    """
    intrinsic = Decimal("0")
    if opt_type.upper() == "CE":
        intrinsic = max(Decimal("0"), spot - strike)
    elif opt_type.upper() == "PE":
        intrinsic = max(Decimal("0"), strike - spot)

    # ATM premium = 1.0% of spot for weekly BankNifty
    atm_prem = spot * Decimal("0.010")

    # Moneyness: how far OTM as fraction of spot
    moneyness = abs(spot - strike) / spot if spot > 0 else Decimal("1")

    # Time factor: matches Nifty common model
    if days_to_expiry <= 1:
        time_factor = Decimal("0.70")
    elif days_to_expiry <= 3:
        time_factor = Decimal("0.90")
    elif days_to_expiry <= 7:
        time_factor = Decimal("1.00")
    else:
        time_factor = Decimal("1.10")

    # OTM discount curve: gentler decay matching Nifty index_option_premium
    otm_discount = Decimal("1") / (Decimal("1") + moneyness * Decimal("45"))

    premium = intrinsic + atm_prem * otm_discount * time_factor
    return max(premium, Decimal("5"))


def estimate_option_premium(
    strike: Decimal,
    spot: Decimal,
    opt_type: str,
    days_to_expiry: int,
    atm_iv: Decimal | None = None,
) -> Decimal:
    """Backward-compatible premium estimator used by tests and old callers."""
    return proxy_premium(strike, spot, opt_type, days_to_expiry)


def evaluate_iron_condor(
    candles: list[Candle],
    *,
    trade_date: date,
    option_contracts: list[dict],
    spot: Decimal,
    vix: Decimal | None = None,
    atm_iv: Decimal | None = None,
    lot_size: int = 30,
    strike_step: Decimal = Decimal("100"),
    max_loss_cap: Decimal = Decimal("18000"),
    min_credit: Decimal = Decimal("200"),
) -> IronCondorSignal | None:
    """Backward-compatible wrapper around the BankNifty iron condor evaluator."""
    if vix is not None and (D(vix) < Decimal("10") or D(vix) > Decimal("25")):
        return None
    return evaluate_bn_iron_condor(
        candles,
        trade_date=trade_date,
        option_contracts=option_contracts,
        spot=spot,
        lot_size=lot_size,
        strike_step=strike_step,
        max_loss_cap=max_loss_cap,
        min_credit=min_credit,
    )


def evaluate_bn_iron_condor(
    candles: list[Candle],
    *,
    trade_date: date,
    option_contracts: list[dict],
    spot: Decimal,
    lot_size: int = 30,
    strike_step: Decimal = Decimal("100"),
    max_loss_cap: Decimal = Decimal("18000"),
    min_credit: Decimal = Decimal("200"),
) -> IronCondorSignal | None:
    """Evaluate BankNifty Iron Condor.

    Entry conditions:
    1. Range day (< 1.0%)
    2. Late entry window after 11:30 IST
    3. Nearest expiry (monthly or weekly) accepted — no strict day limit
    4. Strikes: sell 0.5% OTM, buy 1.5% OTM (~600-pt wings)
    5. Low-body candle with both wicks (no directional conviction)
    6. Net credit ≥ ₹200 minimum
    7. Intraday stop risk capped at ₹18,000
    """
    if not candles or spot <= 0 or len(candles) < 10:
        return None

    latest = candles[-1]

    # Gate 1: Range day filter
    if not is_range_day(candles, spot):
        return None

    # Gate 2: Time window — late entry after 11:30 avoids morning expansion.
    has_entry_window = any(
        time(11, 30) <= c.ts.time() <= time(14, 0)
        for c in candles
    )
    if not has_entry_window:
        return None

    # Gate 3: Find nearest expiry
    expiries = sorted(set(
        c["expiry"] for c in option_contracts
        if isinstance(c.get("expiry"), date) and c["expiry"] > trade_date
    ))
    if not expiries:
        return None
    nearest_expiry = expiries[0]
    days_to_expiry = (nearest_expiry - trade_date).days
    if days_to_expiry < 1:
        return None

    # Gate 4: Available strikes
    available = sorted(set(D(r["strike"]) for r in option_contracts if D(r.get("strike", 0)) > 0))

    # Gate 5: Entry candle — low body with both wicks after 11:30 IST.
    # Strike selection must be anchored to the actual entry, not the latest/spot
    # argument, otherwise a late-day drift can put sold strikes too close or ITM.
    entry_candle: Candle | None = None
    for c in candles:
        t = c.ts.time()
        if t < time(11, 30) or t > time(14, 0):
            continue
        if low_body_candle(c, spot):
            entry_candle = c
            break

    if entry_candle is None:
        return None
    entry_spot = entry_candle.close

    # Gate 6: Strike selection — sell roughly 0.5% OTM, buy roughly 1.5% OTM.
    # BankNifty's 100-point grid means exact percentages are rounded to the
    # nearest available strike on the correct side of the entry price.
    put_candidates = [s for s in available if s < entry_spot]
    call_candidates = [s for s in available if s > entry_spot]
    if not put_candidates or not call_candidates:
        return None
    sold_put = nearest_strike(entry_spot * Decimal("0.995"), put_candidates, strike_step)
    bought_put = nearest_strike(entry_spot * Decimal("0.985"), put_candidates, strike_step)
    sold_call = nearest_strike(entry_spot * Decimal("1.005"), call_candidates, strike_step)
    bought_call = nearest_strike(entry_spot * Decimal("1.015"), call_candidates, strike_step)

    if not (bought_put < sold_put < entry_spot < sold_call < bought_call):
        return None

    wing_put = sold_put - bought_put
    wing_call = bought_call - sold_call
    wing_width = min(wing_put, wing_call)
    if wing_width < strike_step:
        return None

    # Gate 6: Premium estimation
    sp = proxy_premium(sold_put, spot, "PE", days_to_expiry)
    bp = proxy_premium(bought_put, spot, "PE", days_to_expiry)
    put_credit = max(Decimal("0"), sp - bp)

    sc = proxy_premium(sold_call, spot, "CE", days_to_expiry)
    bc = proxy_premium(bought_call, spot, "CE", days_to_expiry)
    call_credit = max(Decimal("0"), sc - bc)

    net_credit = put_credit + call_credit
    if net_credit < min_credit:
        return None

    # Gate 7: Risk check. Structural max loss is wider wing × lot size minus
    # credit. Reject setups that do not fit the configured cap; do not merely
    # clamp the reported risk, because that understates the real worst case.
    wider_wing = max(wing_put, wing_call)
    gross_risk = (wider_wing * D(lot_size)).quantize(TWO_PLACES)
    structural_risk = max(Decimal("0"), gross_risk - net_credit)
    if structural_risk > max_loss_cap:
        return None
    net_risk = structural_risk

    # Exit levels
    stop_upper = sold_call + (bought_call - sold_call) * Decimal("0.35")
    target = spot  # profit when spot stays in range

    metadata = {
        "underlying": "BANKNIFTY",
        "sold_put": float(sold_put),
        "bought_put": float(bought_put),
        "sold_call": float(sold_call),
        "bought_call": float(bought_call),
        "wing_put": float(wing_put),
        "wing_call": float(wing_call),
        "net_credit": float(net_credit),
        "gross_risk": float(gross_risk),
        "structural_risk": float(structural_risk),
        "net_risk": float(net_risk),
        "days_to_expiry": days_to_expiry,
        "expiry": nearest_expiry.isoformat(),
        "spot": float(spot),
        "lot_size": lot_size,
        "put_premium_sold": float(sp),
        "put_premium_bought": float(bp),
        "call_premium_sold": float(sc),
        "call_premium_bought": float(bc),
    }

    return IronCondorSignal(
        strategy_id="banknifty_iron_condor",
        direction="neutral",
        structure="iron_condor",
        entry_time=entry_candle.ts,
        underlying_entry=entry_candle.close,
        sold_put_strike=sold_put,
        bought_put_strike=bought_put,
        sold_call_strike=sold_call,
        bought_call_strike=bought_call,
        net_credit=q2(net_credit),
        max_loss_rupees=q2(net_risk),
        stop_underlying=stop_upper,
        target_underlying=target,
        reason=(
            f"BN IC: Sell {sold_put:.0f}P+{sold_call:.0f}C / "
                    f"Buy {bought_put:.0f}P+{bought_call:.0f}C | "
                    f"Cr ₹{net_credit:.0f} | Risk ₹{net_risk:.0f}"
        ),
        metadata=metadata,
    )


def backtest_iron_condor(
    signal: IronCondorSignal,
    candles: list[Candle],
    *,
    lot_size: int = 30,
) -> dict[str, Any]:
    """Simulate exit P&L from entry candle onwards."""
    entry_idx = next(
        (i for i, c in enumerate(candles) if c.ts >= signal.entry_time),
        -1
    )
    if entry_idx < 0:
        return {"realized_pnl": Decimal("0"), "exit_reason": "no_entry"}

    exit_candle = candles[-1]
    exit_reason = "end_of_data"
    stop_lower = signal.sold_put_strike - (signal.sold_put_strike - signal.bought_put_strike) * Decimal("0.35")

    for c in candles[entry_idx + 1:]:
        t = c.ts.time()
        # Stop: breach upper or lower
        if c.high >= signal.stop_underlying:
            exit_candle = c
            exit_reason = "stop_breach"
            break
        if c.low <= stop_lower:
            exit_candle = c
            exit_reason = "stop_breach"
            break
        # Time exits
        if t >= time(15, 15):
            exit_candle = c
            exit_reason = "time_exit"
            break
        if t >= time(14, 0):
            # After 14:00, take profit if in range
            if signal.sold_put_strike < c.close < signal.sold_call_strike:
                exit_candle = c
                exit_reason = "target_profit"
                break

    exit_spot = exit_candle.close

    # P&L: if spot stayed in range → keep credit (minus 15% friction)
    # If breached → loss proportional to breach depth, capped at max_loss
    if signal.sold_put_strike <= exit_spot <= signal.sold_call_strike:
        realized_pnl = signal.net_credit * Decimal("0.85")
    elif exit_spot < signal.sold_put_strike:
        breach = min(signal.sold_put_strike - exit_spot, signal.sold_put_strike - signal.bought_put_strike)
        realized_pnl = -(breach * D(lot_size)).quantize(TWO_PLACES)
    else:
        breach = min(exit_spot - signal.sold_call_strike, signal.bought_call_strike - signal.sold_call_strike)
        realized_pnl = -(breach * D(lot_size)).quantize(TWO_PLACES)

    realized_pnl = max(realized_pnl, -signal.max_loss_rupees)

    return {
        "exit_time": exit_candle.ts,
        "exit_underlying": exit_spot,
        "realized_pnl": q2(max(realized_pnl, -signal.max_loss_rupees)),
        "exit_reason": exit_reason,
    }