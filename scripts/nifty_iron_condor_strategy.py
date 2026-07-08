#!/usr/bin/env python3
"""Nifty Iron Condor v1 — pure paper/proxy strategy logic.

Neutral NIFTY index-options credit spread for range days. No broker APIs.
Backtests use underlying 5-minute candles with proxy option pricing because
expired option-chain candles are not available in the local dataset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal
from typing import Any

from scripts.proxy_backtest_common import Candle, D, index_option_premium, intraday_range_pct, q2, round_to_step


@dataclass(frozen=True)
class NiftyIronCondorSignal:
    strategy_id: str
    direction: str
    structure: str
    entry_time: Any
    underlying_entry: Decimal
    sold_put_strike: Decimal
    bought_put_strike: Decimal
    sold_call_strike: Decimal
    bought_call_strike: Decimal
    net_credit: Decimal
    max_loss_rupees: Decimal
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _select_entry_candle(candles: list[Candle], entry_after: time) -> Candle | None:
    """Pick first quiet candle at/after the late-entry gate."""
    for candle in candles:
        if candle.ts.time() < entry_after or candle.ts.time() > time(13, 30):
            continue
        body = abs(candle.close - candle.open)
        rng = max(Decimal("1"), candle.high - candle.low)
        if body / rng <= Decimal("0.45"):
            return candle
    return next((c for c in candles if c.ts.time() >= entry_after), None)


def evaluate_nifty_iron_condor(
    candles: list[Candle],
    *,
    trade_date: date,
    spot: Decimal | None = None,
    lot_size: int = 25,
    strike_step: Decimal = Decimal("50"),
    max_loss_cap: Decimal = Decimal("7500"),
    min_credit: Decimal = Decimal("100"),
    range_threshold: Decimal = Decimal("0.010"),
    entry_after: time = time(11, 30),
) -> NiftyIronCondorSignal | None:
    if len(candles) < 20:
        return None
    spot = D(spot or candles[-1].close)
    if spot <= 0:
        return None
    if intraday_range_pct(candles, until=entry_after) > range_threshold:
        return None

    entry = _select_entry_candle(candles, entry_after)
    if entry is None:
        return None

    entry_spot = entry.close
    atm = round_to_step(entry_spot, strike_step)
    # Sell one step OTM, buy three more steps away: 3-step wings = 150pts.
    sold_put = atm - strike_step
    bought_put = sold_put - strike_step * Decimal("3")
    sold_call = atm + strike_step
    bought_call = sold_call + strike_step * Decimal("3")

    days_to_expiry = max(1, 7 - trade_date.weekday())
    put_credit = max(
        Decimal("0"),
        index_option_premium(entry_spot, sold_put, "PE", underlying="NIFTY", days_to_expiry=days_to_expiry)
        - index_option_premium(entry_spot, bought_put, "PE", underlying="NIFTY", days_to_expiry=days_to_expiry),
    )
    call_credit = max(
        Decimal("0"),
        index_option_premium(entry_spot, sold_call, "CE", underlying="NIFTY", days_to_expiry=days_to_expiry)
        - index_option_premium(entry_spot, bought_call, "CE", underlying="NIFTY", days_to_expiry=days_to_expiry),
    )
    net_credit = q2(put_credit + call_credit)
    if net_credit < min_credit:
        return None

    wing_width = max(sold_put - bought_put, bought_call - sold_call)
    structural_risk = q2(wing_width * D(lot_size) - net_credit)
    risk = q2(min(max_loss_cap, max(Decimal("0"), structural_risk)))
    if risk <= 0:
        return None

    return NiftyIronCondorSignal(
        strategy_id="nifty_iron_condor",
        direction="neutral",
        structure="iron_condor",
        entry_time=entry.ts,
        underlying_entry=entry_spot,
        sold_put_strike=sold_put,
        bought_put_strike=bought_put,
        sold_call_strike=sold_call,
        bought_call_strike=bought_call,
        net_credit=net_credit,
        max_loss_rupees=risk,
        reason=(
            f"NIFTY IC late range: sell {sold_put:.0f}P/{sold_call:.0f}C "
            f"buy {bought_put:.0f}P/{bought_call:.0f}C credit ₹{net_credit:.0f}"
        ),
        metadata={
            "trade_date": trade_date.isoformat(),
            "lot_size": lot_size,
            "strike_step": float(strike_step),
            "wing_width": float(wing_width),
            "structural_risk": float(structural_risk),
            "range_pct_until_entry": float(intraday_range_pct(candles, until=entry_after)),
            "days_to_expiry": days_to_expiry,
        },
    )


def backtest_nifty_iron_condor(signal: NiftyIronCondorSignal, candles: list[Candle], *, lot_size: int = 25) -> dict[str, Any]:
    entry_idx = next((i for i, c in enumerate(candles) if c.ts >= signal.entry_time), -1)
    if entry_idx < 0:
        return {"exit_time": signal.entry_time, "exit_underlying": signal.underlying_entry, "pnl": Decimal("0"), "exit_reason": "no_entry"}

    exit_candle = candles[-1]
    exit_reason = "force_intraday_exit"
    stop_upper = signal.sold_call_strike + (signal.bought_call_strike - signal.sold_call_strike) * Decimal("0.35")
    stop_lower = signal.sold_put_strike - (signal.sold_put_strike - signal.bought_put_strike) * Decimal("0.35")

    for candle in candles[entry_idx + 1:]:
        if candle.high >= stop_upper:
            exit_candle, exit_reason = candle, "stop_call_side"
            break
        if candle.low <= stop_lower:
            exit_candle, exit_reason = candle, "stop_put_side"
            break
        if candle.ts.time() >= time(14, 30) and signal.sold_put_strike < candle.close < signal.sold_call_strike:
            exit_candle, exit_reason = candle, "target_credit_decay"
            break
        if candle.ts.time() >= time(15, 15):
            exit_candle, exit_reason = candle, "force_intraday_exit"
            break

    if signal.sold_put_strike <= exit_candle.close <= signal.sold_call_strike:
        pnl = q2(signal.net_credit * Decimal("0.70"))
    elif exit_candle.close < signal.sold_put_strike:
        breach = min(signal.sold_put_strike - exit_candle.close, signal.sold_put_strike - signal.bought_put_strike)
        pnl = -q2(breach * D(lot_size))
    else:
        breach = min(exit_candle.close - signal.sold_call_strike, signal.bought_call_strike - signal.sold_call_strike)
        pnl = -q2(breach * D(lot_size))
    pnl = max(pnl, -signal.max_loss_rupees)
    return {"exit_time": exit_candle.ts, "exit_underlying": exit_candle.close, "pnl": q2(pnl), "exit_reason": exit_reason}
