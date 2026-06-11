#!/usr/bin/env python3
"""Deterministic paper-only intraday NSE options strategy pack.

Research/paper only. This module contains pure signal/risk logic and deliberately
contains no FYERS order APIs. Live orders are forbidden by config validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import json
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
class StrategyRuntimeConfig:
    strategy_id: str
    name: str
    enabled: bool = True
    paper_trade_enabled: bool = True
    paper_capital: Decimal = Decimal("50000")
    max_trade_loss: Decimal = Decimal("1500")
    max_daily_loss: Decimal = Decimal("5000")
    max_premium_exposure: Decimal = Decimal("40000")
    max_trades_per_day: int = 3
    max_open_positions: int = 1


@dataclass(frozen=True)
class StrategyPackConfig:
    paper_only: bool
    live_orders_enabled: bool
    strategies: dict[str, StrategyRuntimeConfig] = field(default_factory=dict)
    force_exit_time: str = "15:20"
    no_new_entries_before: str = "09:30"
    global_max_open_positions: int = 5
    notes: str = "Paper-only NSE intraday options strategy pack. No live orders."

    def validate(self) -> None:
        if self.paper_only is not True:
            raise ValueError("paper_only must be true")
        if self.live_orders_enabled is not False:
            raise ValueError("live_orders_enabled must be false")
        if not self.strategies:
            raise ValueError("at least one strategy must be configured")
        for key, value in (("force_exit_time", self.force_exit_time), ("no_new_entries_before", self.no_new_entries_before)):
            try:
                time.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} is not a valid HH:MM time: {value!r}") from exc
        for sid, cfg in self.strategies.items():
            if not cfg.paper_trade_enabled:
                continue
            if cfg.paper_capital != Decimal("50000"):
                raise ValueError(f"{sid} paper_capital must be 50000 for the one-month test")
            if cfg.max_trade_loss > Decimal("1500"):
                raise ValueError(f"{sid} max_trade_loss exceeds 1500")
            if cfg.max_premium_exposure > Decimal("40000"):
                raise ValueError(f"{sid} max_premium_exposure exceeds 40000")


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    direction: str
    structure: str
    entry_time: datetime
    underlying_entry: Decimal
    reason: str
    max_loss_rupees: Decimal
    stop_loss_rupees: Decimal = Decimal("0")
    target_r: Decimal = Decimal("2")
    metadata: dict[str, Any] = field(default_factory=dict)


DEFAULT_STRATEGIES = {
    "nifty_orb_debit_spread": "Nifty ORB Debit Spread",
    "cpr_trend_debit_spread": "CPR Trend-Day Debit Spread",
    "expiry_tuesday_directional": "Expiry Tuesday Nifty Defined-Risk Directional",
    "nifty_vwap_mean_reversion": "Nifty VWAP Mean Reversion Long",
    "single_stock_momentum_index_confirm": "Single-Stock Momentum with Index Confirmation",
}


def D(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def strict_bool(value: Any, *, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be a JSON boolean, got {value!r}")


def build_default_config() -> StrategyPackConfig:
    strategies = {
        sid: StrategyRuntimeConfig(strategy_id=sid, name=name)
        for sid, name in DEFAULT_STRATEGIES.items()
    }
    cfg = StrategyPackConfig(paper_only=True, live_orders_enabled=False, strategies=strategies)
    cfg.validate()
    return cfg


def config_to_json_dict(cfg: StrategyPackConfig) -> dict[str, Any]:
    return {
        "paper_only": cfg.paper_only,
        "live_orders_enabled": cfg.live_orders_enabled,
        "force_exit_time": cfg.force_exit_time,
        "no_new_entries_before": cfg.no_new_entries_before,
        "global_max_open_positions": cfg.global_max_open_positions,
        "notes": cfg.notes,
        "strategies": {
            sid: {
                "name": s.name,
                "enabled": s.enabled,
                "paper_trade_enabled": s.paper_trade_enabled,
                "paper_capital": str(s.paper_capital),
                "max_trade_loss": str(s.max_trade_loss),
                "max_daily_loss": str(s.max_daily_loss),
                "max_premium_exposure": str(s.max_premium_exposure),
                "max_trades_per_day": s.max_trades_per_day,
                "max_open_positions": s.max_open_positions,
            }
            for sid, s in cfg.strategies.items()
        },
    }


def save_default_config(path: Path) -> None:
    cfg = build_default_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config_to_json_dict(cfg), indent=2), encoding="utf-8")


def load_config(path: Path) -> StrategyPackConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    strategies: dict[str, StrategyRuntimeConfig] = {}
    for sid, raw in (data.get("strategies") or {}).items():
        strategies[sid] = StrategyRuntimeConfig(
            strategy_id=sid,
            name=raw.get("name", sid),
            enabled=strict_bool(raw.get("enabled", True), key=f"strategies.{sid}.enabled"),
            paper_trade_enabled=strict_bool(raw.get("paper_trade_enabled", True), key=f"strategies.{sid}.paper_trade_enabled"),
            paper_capital=D(raw.get("paper_capital", "50000")),
            max_trade_loss=D(raw.get("max_trade_loss", "1500")),
            max_daily_loss=D(raw.get("max_daily_loss", "5000")),
            max_premium_exposure=D(raw.get("max_premium_exposure", "40000")),
            max_trades_per_day=int(raw.get("max_trades_per_day", 3)),
            max_open_positions=int(raw.get("max_open_positions", 1)),
        )
    cfg = StrategyPackConfig(
        paper_only=strict_bool(data.get("paper_only", True), key="paper_only"),
        live_orders_enabled=strict_bool(data.get("live_orders_enabled", False), key="live_orders_enabled"),
        strategies=strategies,
        force_exit_time=data.get("force_exit_time", "15:20"),
        no_new_entries_before=data.get("no_new_entries_before", "09:30"),
        global_max_open_positions=int(data.get("global_max_open_positions", 5)),
        notes=data.get("notes", "Paper-only NSE intraday options strategy pack. No live orders."),
    )
    cfg.validate()
    return cfg


def in_time_window(ts: datetime, start: time, end: time) -> bool:
    t = ts.time()
    return start <= t <= end


def avg(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def check_debit_spread_risk(net_debit_per_share: Decimal, *, lot_size: int, max_loss: Decimal = Decimal("1500")) -> bool:
    debit = D(net_debit_per_share)
    if debit <= 0:
        return False
    return debit * Decimal(lot_size) <= max_loss


def next_tuesday_expiry(day: date) -> date:
    days_ahead = (1 - day.weekday()) % 7  # Monday=0, Tuesday=1
    return day + timedelta(days=days_ahead)


def opening_range(candles: list[Candle], *, start: time = time(9, 15), end: time = time(9, 45)) -> tuple[Decimal, Decimal, list[Candle]]:
    rows = [c for c in candles if start <= c.ts.time() < end]
    if not rows:
        return Decimal("0"), Decimal("0"), []
    return max(c.high for c in rows), min(c.low for c in rows), rows


def breached_both_sides(rows: list[Candle], high: Decimal, low: Decimal) -> bool:
    """True when the given candles traded strictly beyond both the high and the low."""
    return any(c.high > high for c in rows) and any(c.low < low for c in rows)


def or_formation_whipsaw(or_rows: list[Candle], *, split: time = time(9, 30)) -> bool:
    """Card filter: skip if the opening range was breached both sides before 09:45.

    The final OR levels cannot be breached during their own formation, so the
    deterministic reading is: candles after the 09:15-09:30 provisional range
    escaping both of its sides marks an indecision/whipsaw open.
    """
    early = [c for c in or_rows if c.ts.time() < split]
    late = [c for c in or_rows if c.ts.time() >= split]
    if not early or not late:
        return False
    return breached_both_sides(late, max(c.high for c in early), min(c.low for c in early))


def volume_confirm(candle: Candle, prior_rows: list[Candle], multiple: Decimal) -> bool:
    vols = [Decimal(c.volume) for c in prior_rows[-6:] if c.volume > 0]
    if not vols:
        return True
    return Decimal(candle.volume) >= avg(vols) * multiple


def evaluate_nifty_orb_debit_spread(
    candles: list[Candle],
    *,
    vix: Decimal,
    net_debit_per_share: Decimal,
    lot_size: int,
    max_trade_loss: Decimal = Decimal("1500"),
) -> StrategySignal | None:
    if not (Decimal("10") <= D(vix) <= Decimal("22")):
        return None
    or_high, or_low, or_rows = opening_range(candles)
    if not or_rows:
        return None
    spot = or_rows[-1].close
    width_pct = (or_high - or_low) / spot if spot else Decimal("0")
    if width_pct < Decimal("0.0025") or width_pct > Decimal("0.012"):
        return None
    if or_formation_whipsaw(or_rows):
        return None
    if not check_debit_spread_risk(D(net_debit_per_share), lot_size=lot_size, max_loss=max_trade_loss):
        return None
    metadata = {
        "or_high": str(or_high),
        "or_low": str(or_low),
        "width_pct": str(width_pct),
        "net_debit_per_share": str(D(net_debit_per_share)),
        "lot_size": lot_size,
    }
    for candle in candles:
        if not in_time_window(candle.ts, time(9, 45), time(13, 30)):
            continue
        prior = [x for x in candles if x.ts < candle.ts]
        post_or = [x for x in prior if x.ts.time() >= time(9, 45)]
        if breached_both_sides(post_or, or_high, or_low):
            return None
        if candle.close > or_high and volume_confirm(candle, prior, Decimal("1.5")):
            return StrategySignal(
                strategy_id="nifty_orb_debit_spread",
                direction="long",
                structure="bull_call_debit_spread",
                entry_time=candle.ts,
                underlying_entry=candle.close,
                reason="5m close above opening range with volume confirmation",
                max_loss_rupees=(D(net_debit_per_share) * Decimal(lot_size)).quantize(TWO_PLACES),
                stop_loss_rupees=Decimal("1200"),
                metadata=metadata,
            )
        if candle.close < or_low and volume_confirm(candle, prior, Decimal("1.5")):
            return StrategySignal(
                strategy_id="nifty_orb_debit_spread",
                direction="short",
                structure="bear_put_debit_spread",
                entry_time=candle.ts,
                underlying_entry=candle.close,
                reason="5m close below opening range with volume confirmation",
                max_loss_rupees=(D(net_debit_per_share) * Decimal(lot_size)).quantize(TWO_PLACES),
                stop_loss_rupees=Decimal("1200"),
                metadata=metadata,
            )
    return None


def cpr_from_previous_day(previous_day: list[Candle]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    high = max(c.high for c in previous_day)
    low = min(c.low for c in previous_day)
    close = previous_day[-1].close
    pivot = (high + low + close) / Decimal("3")
    bc = (high + low) / Decimal("2")
    tc = Decimal("2") * pivot - bc
    return pivot, min(bc, tc), max(bc, tc), close


def evaluate_cpr_trend_debit_spread(
    candles: list[Candle],
    *,
    previous_day: list[Candle],
    underlying: str,
    vix: Decimal,
    net_debit_per_share: Decimal,
    lot_size: int,
    sessions_to_expiry: int,
    max_trade_loss: Decimal = Decimal("1500"),
) -> StrategySignal | None:
    if not previous_day or not (Decimal("10") <= D(vix) <= Decimal("24")):
        return None
    if underlying.upper() == "BANKNIFTY" and sessions_to_expiry <= 3:
        return None
    if not check_debit_spread_risk(D(net_debit_per_share), lot_size=lot_size, max_loss=max_trade_loss):
        return None
    pivot, bc, tc, prev_close = cpr_from_previous_day(previous_day)
    threshold = Decimal("0.0035") if underlying.upper() == "BANKNIFTY" else Decimal("0.003")
    width_pct = abs(tc - bc) / prev_close if prev_close else Decimal("0")
    if width_pct > threshold:
        return None
    # Card: bias comes from the first 15-minute close, i.e. the close of the
    # last 5m candle before 09:30 once the full 09:15-09:30 window has printed.
    first15_rows = [c for c in candles if time(9, 15) <= c.ts.time() < time(9, 30)]
    if len(first15_rows) < 3:
        return None
    first15_close = first15_rows[-1].close
    bias = "long" if first15_close > tc else "short" if first15_close < bc else "none"
    if bias == "none":
        return None
    prev_high = max(c.high for c in previous_day)
    prev_low = min(c.low for c in previous_day)
    for candle in candles:
        if not in_time_window(candle.ts, time(9, 45), time(13, 30)):
            continue
        if bias == "long" and candle.close > prev_high:
            return StrategySignal(
                strategy_id="cpr_trend_debit_spread",
                direction="long",
                structure="bull_call_debit_spread",
                entry_time=candle.ts,
                underlying_entry=candle.close,
                reason="narrow CPR trend day long break beyond previous high",
                max_loss_rupees=(D(net_debit_per_share) * Decimal(lot_size)).quantize(TWO_PLACES),
                stop_loss_rupees=Decimal("1200"),
                metadata={"pivot": str(pivot), "bc": str(bc), "tc": str(tc), "width_pct": str(width_pct), "net_debit_per_share": str(D(net_debit_per_share)), "lot_size": lot_size},
            )
        if bias == "short" and candle.close < prev_low:
            return StrategySignal(
                strategy_id="cpr_trend_debit_spread",
                direction="short",
                structure="bear_put_debit_spread",
                entry_time=candle.ts,
                underlying_entry=candle.close,
                reason="narrow CPR trend day short break beyond previous low",
                max_loss_rupees=(D(net_debit_per_share) * Decimal(lot_size)).quantize(TWO_PLACES),
                stop_loss_rupees=Decimal("1200"),
                metadata={"pivot": str(pivot), "bc": str(bc), "tc": str(tc), "width_pct": str(width_pct), "net_debit_per_share": str(D(net_debit_per_share)), "lot_size": lot_size},
            )
    return None


def evaluate_expiry_tuesday_directional(
    candles: list[Candle],
    *,
    trade_date: date,
    vix: Decimal,
    option_premium: Decimal,
    lot_size: int,
    max_trade_loss: Decimal = Decimal("1500"),
) -> StrategySignal | None:
    if trade_date.weekday() != 1 or D(vix) > Decimal("24"):
        return None
    or_high, or_low, or_rows = opening_range(candles)
    if not or_rows:
        return None
    spot = or_rows[-1].close
    if ((or_high - or_low) / spot if spot else Decimal("0")) < Decimal("0.002"):
        return None
    stop_per_share = min(D(option_premium) * Decimal("0.30"), Decimal("22.3"))
    stop_rupees = (stop_per_share * Decimal(lot_size)).quantize(TWO_PLACES)
    if stop_rupees > max_trade_loss:
        return None
    for candle in candles:
        if not in_time_window(candle.ts, time(9, 45), time(12, 30)):
            continue
        prior = [x for x in candles if x.ts < candle.ts]
        if candle.close > or_high and volume_confirm(candle, prior, Decimal("1.5")):
            return StrategySignal(
                strategy_id="expiry_tuesday_directional",
                direction="long",
                structure="long_atm_ce",
                entry_time=candle.ts,
                underlying_entry=candle.close,
                reason="Tuesday expiry OR breakout with volume confirmation",
                max_loss_rupees=stop_rupees,
                stop_loss_rupees=stop_rupees,
                target_r=Decimal("1"),
            )
        if candle.close < or_low and volume_confirm(candle, prior, Decimal("1.5")):
            return StrategySignal(
                strategy_id="expiry_tuesday_directional",
                direction="short",
                structure="long_atm_pe",
                entry_time=candle.ts,
                underlying_entry=candle.close,
                reason="Tuesday expiry OR breakdown with volume confirmation",
                max_loss_rupees=stop_rupees,
                stop_loss_rupees=stop_rupees,
                target_r=Decimal("1"),
            )
    return None


def session_vwap(candles: list[Candle]) -> Decimal:
    pv = Decimal("0")
    vol = Decimal("0")
    for c in candles:
        typical = (c.high + c.low + c.close) / Decimal("3")
        v = Decimal(c.volume or 1)
        pv += typical * v
        vol += v
    return pv / vol if vol else candles[-1].close


def stddev(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal("0")
    mean = avg(values)
    var = sum((x - mean) * (x - mean) for x in values) / Decimal(len(values))
    # Decimal sqrt is available in py3.11+
    return var.sqrt()


def bullish_rejection(candle: Candle) -> bool:
    body_low = min(candle.open, candle.close)
    lower_wick = body_low - candle.low
    body = abs(candle.close - candle.open)
    return candle.close > candle.open and lower_wick >= body


def bearish_rejection(candle: Candle) -> bool:
    body_high = max(candle.open, candle.close)
    upper_wick = candle.high - body_high
    body = abs(candle.close - candle.open)
    return candle.close < candle.open and upper_wick >= body


def evaluate_nifty_vwap_mean_reversion(
    candles: list[Candle],
    *,
    is_range_day: bool,
    is_cpr_narrow: bool,
    vix: Decimal,
    rsi9: Decimal,
    option_premium: Decimal,
    lot_size: int,
    max_trade_loss: Decimal = Decimal("1500"),
) -> StrategySignal | None:
    if not is_range_day or is_cpr_narrow or D(vix) > Decimal("20") or not candles:
        return None
    latest = candles[-1]
    if not in_time_window(latest.ts, time(9, 50), time(14, 0)):
        return None
    vwap = session_vwap(candles)
    sigma = stddev([c.close for c in candles])
    lower = vwap - Decimal("2.0") * sigma
    upper = vwap + Decimal("2.0") * sigma
    stop_per_share = min(Decimal("22.3"), D(option_premium) * Decimal("0.25"))
    stop_rupees = (stop_per_share * Decimal(lot_size)).quantize(TWO_PLACES)
    if stop_rupees > max_trade_loss:
        return None
    if latest.low <= lower and bullish_rejection(latest) and Decimal("30") <= D(rsi9) <= Decimal("45"):
        return StrategySignal(
            strategy_id="nifty_vwap_mean_reversion",
            direction="long_ce",
            structure="long_atm_ce",
            entry_time=latest.ts,
            underlying_entry=latest.close,
            reason="range-day lower VWAP band rejection",
            max_loss_rupees=stop_rupees,
            stop_loss_rupees=stop_rupees,
            target_r=Decimal("1.2"),
            metadata={"vwap": str(vwap), "lower": str(lower), "upper": str(upper)},
        )
    if latest.high >= upper and bearish_rejection(latest) and Decimal("55") <= D(rsi9) <= Decimal("70"):
        return StrategySignal(
            strategy_id="nifty_vwap_mean_reversion",
            direction="long_pe",
            structure="long_atm_pe",
            entry_time=latest.ts,
            underlying_entry=latest.close,
            reason="range-day upper VWAP band rejection",
            max_loss_rupees=stop_rupees,
            stop_loss_rupees=stop_rupees,
            target_r=Decimal("1.2"),
            metadata={"vwap": str(vwap), "lower": str(lower), "upper": str(upper)},
        )
    return None


def evaluate_single_stock_momentum(
    stock_candles: list[Candle],
    index_candles: list[Candle],
    *,
    stock_symbol: str,
    confirming_index: str,
    vix: Decimal,
    option_spread_pct: Decimal,
    net_debit_per_share: Decimal,
    lot_size: int,
    earnings_today: bool,
    stock_intraday_pct: Decimal,
    index_intraday_pct: Decimal,
    max_trade_loss: Decimal = Decimal("1500"),
) -> StrategySignal | None:
    if earnings_today or not (Decimal("10") <= D(vix) <= Decimal("24")):
        return None
    if D(option_spread_pct) > Decimal("0.005"):
        return None
    if not check_debit_spread_risk(D(net_debit_per_share), lot_size=lot_size, max_loss=max_trade_loss):
        return None
    stock_sig = evaluate_nifty_orb_debit_spread(stock_candles, vix=D(vix), net_debit_per_share=D(net_debit_per_share), lot_size=lot_size, max_trade_loss=max_trade_loss)
    index_sig = evaluate_nifty_orb_debit_spread(index_candles, vix=D(vix), net_debit_per_share=Decimal("1"), lot_size=1)
    if not stock_sig or not index_sig or stock_sig.direction != index_sig.direction:
        return None
    # Relative strength must point in the trade direction: longs need the stock
    # outperforming the index, shorts need it underperforming.
    relative_strength = D(stock_intraday_pct) - D(index_intraday_pct)
    if stock_sig.direction == "long" and relative_strength < Decimal("0.2"):
        return None
    if stock_sig.direction == "short" and relative_strength > Decimal("-0.2"):
        return None
    return StrategySignal(
        strategy_id="single_stock_momentum_index_confirm",
        direction=stock_sig.direction,
        structure="stock_option_debit_spread",
        entry_time=stock_sig.entry_time,
        underlying_entry=stock_sig.underlying_entry,
        reason=f"{stock_symbol} OR breakout confirmed by {confirming_index} and relative strength",
        max_loss_rupees=(D(net_debit_per_share) * Decimal(lot_size)).quantize(TWO_PLACES),
        stop_loss_rupees=Decimal("1300"),
        metadata={"stock_symbol": stock_symbol, "confirming_index": confirming_index, "net_debit_per_share": str(D(net_debit_per_share)), "lot_size": lot_size, "relative_strength": str(relative_strength)},
    )


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
