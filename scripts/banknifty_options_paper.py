#!/usr/bin/env python3
"""BankNifty options paper campaign.

Safety stance:
- Paper trading only.
- Long CE/PE options only; no option selling.
- No FYERS order placement, modification, cancellation, or exit calls.
- Uses read-only FYERS quotes and local PostgreSQL audit tables.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "banknifty_options_paper.json"
DEFAULT_DATABASE_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"
FYERS_NSE_FO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
IST = ZoneInfo("Asia/Kolkata")
TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class FyersOptionContract:
    symbol: str
    underlying: str
    expiry: date
    strike: Decimal
    option_type: str
    lot_size: int
    tick_size: Decimal
    raw: dict[str, Any]


@dataclass(frozen=True)
class BankNiftyConstituent:
    symbol: str
    fyers_symbol: str
    weight: Decimal | None = None


@dataclass(frozen=True)
class ConstituentMove:
    symbol: str
    fyers_symbol: str
    ltp: Decimal
    open: Decimal
    pct_from_open: Decimal
    normalized_weight: Decimal
    contribution: Decimal
    vwap: Decimal | None = None
    volume: int | None = None
    relative_volume: Decimal | None = None


@dataclass(frozen=True)
class ConstituentJumpReason:
    symbol: str
    direction: str
    pct_from_open: Decimal
    contribution: Decimal
    vwap_confirmed: bool
    relative_volume_confirmed: bool
    summary: str


@dataclass(frozen=True)
class ConfirmationDecision:
    allowed: bool
    reasons: list[str]
    confirmed_symbols: list[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class IndexStructureSignal:
    confirmed: bool
    reason: str
    stop_level: Decimal | None
    reference_level: Decimal | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class DirectionSignal:
    direction: str | None
    reason: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class StrategyCardRule:
    strategy_id: str
    name: str
    enabled: bool
    paper_trade_enabled: bool
    entry_function: str
    status: str = "research_only"
    source: str = ""
    notes: str = ""
    card_type: str = "unspecified"


@dataclass(frozen=True)
class RiskFilterConfig:
    """Deterministic, paper-only option risk/guardrail filter settings.

    All thresholds are auditable and config-driven. A threshold of 0 (rupee/pct
    fields) means that individual check is disabled. require_* flags stay false
    until a Greeks/IV data source is wired so the filter never blocks on data we
    do not yet ingest.
    """
    enabled: bool = False
    enforce_spread_filter: bool = True
    max_spread_pct: Decimal = Decimal("3.0")
    max_spread_rupees: Decimal = Decimal("5.0")
    min_volume: int = 0
    min_oi: int = 0
    require_greeks: bool = False
    min_abs_delta: Decimal = Decimal("0.25")
    max_abs_theta: Decimal = Decimal("0")
    max_iv: Decimal = Decimal("0")
    require_iv: bool = False


@dataclass(frozen=True)
class ChainSignalConfig:
    """Option-chain-derived entry confirmation/veto settings.

    Every block_* flag defaults False so the gate is advisory: contradicting chain
    context is recorded on the trade but never blocks an entry until the operator
    promotes a specific check to blocking after observing the logs. A 0 threshold
    disables that individual check."""
    enabled: bool = False
    block_on_iv_regime_high: bool = False
    block_on_contradicting_oi: bool = False
    block_on_pcr_extreme: bool = False
    pcr_bullish_max: Decimal = Decimal("0")   # CE entry: PCR above this is a bearish-skew contradiction
    pcr_bearish_min: Decimal = Decimal("0")   # PE entry: PCR below this is a bullish-skew contradiction


@dataclass(frozen=True)
class OptionQuoteMetrics:
    bid: Decimal | None
    ask: Decimal | None
    spread: Decimal | None
    spread_pct: Decimal | None
    volume: int | None
    oi: int | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    iv: Decimal | None

    def as_raw(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "bid": s(self.bid),
            "ask": s(self.ask),
            "spread": s(self.spread),
            "spread_pct": s(self.spread_pct),
            "volume": self.volume,
            "oi": self.oi,
            "delta": s(self.delta),
            "gamma": s(self.gamma),
            "theta": s(self.theta),
            "vega": s(self.vega),
            "iv": s(self.iv),
        }


@dataclass(frozen=True)
class RiskFilterDecision:
    allowed: bool
    reasons: list[str]
    warnings: list[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class ChainSignalDecision:
    allowed: bool
    reasons: list[str]
    warnings: list[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class RealisticRiskPlan:
    stop_premium: Decimal
    target_premium: Decimal
    index_stop: Decimal | None
    risk_points: Decimal
    risk_rupees: Decimal
    target_points: Decimal
    raw: dict[str, Any]


@dataclass(frozen=True)
class CampaignConfig:
    campaign_name: str
    strategy_version: str
    underlying: str
    underlying_symbol: str
    starting_capital: Decimal
    max_premium_exposure: Decimal
    max_daily_loss: Decimal
    max_open_positions: int
    max_trades_per_day: int
    max_trade_loss: Decimal
    stop_loss_pct: Decimal
    target_pct: Decimal
    fixed_target_exit_enabled: bool
    realistic_risk_enabled: bool
    structure_candle_resolution: str
    option_structure_lookback_candles: int
    atr_buffer_multiplier: Decimal
    target_r_multiple: Decimal
    profit_lock_trigger: Decimal
    profit_lock_step: Decimal
    option_tick_size: Decimal
    strike_step: Decimal
    signal_threshold_pct: Decimal
    min_index_confirmation_pct: Decimal
    min_constituent_coverage_pct: Decimal
    min_directional_weight_pct: Decimal
    index_structure_confirmation_enabled: bool
    index_structure_lookback_candles: int
    index_structure_breakout_buffer_pct: Decimal
    index_structure_stop_buffer_pct: Decimal
    swing_trailing_enabled: bool
    vwap_volume_confirmation_enabled: bool
    min_vwap_volume_confirming_top_movers: int
    relative_volume_threshold: Decimal
    major_jump_threshold_pct: Decimal
    weighted_vwap_side_pct: Decimal
    no_new_trades_before: dtime
    lunch_window_start: dtime
    lunch_window_end: dtime
    lunch_min_day_range_vs_adr10: Decimal
    lunch_min_relvol: Decimal
    expiry_pm_min_relvol: Decimal
    chop_lookback_candles: int
    chop_max_net_move_pct: Decimal
    chop_max_vwap_crosses: int
    leg_lookback_candles: int
    confluence_levels: tuple[str, ...]
    pullback_max_candles: int
    pullback_level_hold_buffer_pct: Decimal
    beta_lookback_min: int
    beta_fallback_atm: Decimal
    beta_fallback_otm1: Decimal
    breakeven_at_r: Decimal
    ratchet_start_r: Decimal
    ratchet_giveback_pct: Decimal
    ratchet_giveback_min_inr: Decimal
    stagnation_minutes: int
    stagnation_min_r: Decimal
    constituents: tuple[BankNiftyConstituent, ...]
    no_new_trades_after: dtime
    force_exit_time: dtime
    entry_scan_interval_minutes: int
    open_position_update_interval_minutes: int
    poll_interval_seconds: int
    quote_stale_seconds: int
    chain_stale_seconds: int
    chain_selection_enabled: bool
    paper_only: bool
    live_orders_enabled: bool
    notes: str
    strategy_router: tuple[StrategyCardRule, ...]
    risk_filter: RiskFilterConfig
    chain_signals: ChainSignalConfig


@dataclass(frozen=True)
class Campaign:
    campaign_id: int
    name: str
    start_date: date
    starting_capital: Decimal
    max_daily_loss: Decimal
    max_open_positions: int
    max_trades_per_day: int


def money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"₹{value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def json_dumps_safe(value: Any) -> str:
    """Serialize DB/FYERS metadata that may contain Decimal or datetime values."""
    def default(obj: Any) -> str:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, (date, datetime, dtime)):
            return obj.isoformat()
        return str(obj)

    return json.dumps(value, default=default)


def parse_time(value: str) -> dtime:
    hh, mm = value.split(":", 1)
    return dtime(int(hh), int(mm), tzinfo=IST)


def default_banknifty_constituents() -> tuple[BankNiftyConstituent, ...]:
    symbols = (
        ("AUBANK", "NSE:AUBANK-EQ"),
        ("AXISBANK", "NSE:AXISBANK-EQ"),
        ("BANKBARODA", "NSE:BANKBARODA-EQ"),
        ("CANBK", "NSE:CANBK-EQ"),
        ("FEDERALBNK", "NSE:FEDERALBNK-EQ"),
        ("HDFCBANK", "NSE:HDFCBANK-EQ"),
        ("ICICIBANK", "NSE:ICICIBANK-EQ"),
        ("IDFCFIRSTB", "NSE:IDFCFIRSTB-EQ"),
        ("INDUSINDBK", "NSE:INDUSINDBK-EQ"),
        ("KOTAKBANK", "NSE:KOTAKBANK-EQ"),
        ("PNB", "NSE:PNB-EQ"),
        ("SBIN", "NSE:SBIN-EQ"),
        ("UNIONBANK", "NSE:UNIONBANK-EQ"),
        ("YESBANK", "NSE:YESBANK-EQ"),
    )
    return tuple(BankNiftyConstituent(symbol=s, fyers_symbol=f, weight=None) for s, f in symbols)


def parse_constituents(raw: Any) -> tuple[BankNiftyConstituent, ...]:
    if not isinstance(raw, list) or not raw:
        return default_banknifty_constituents()
    parsed: list[BankNiftyConstituent] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        fyers_symbol = str(item.get("fyers_symbol") or f"NSE:{symbol}-EQ").strip()
        weight_raw = item.get("weight")
        weight = None if weight_raw in (None, "", 0, "0") else Decimal(str(weight_raw))
        if symbol and fyers_symbol:
            parsed.append(BankNiftyConstituent(symbol=symbol, fyers_symbol=fyers_symbol, weight=weight))
    return tuple(parsed) if parsed else default_banknifty_constituents()


def bool_from_config(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    return bool(value)


def strict_bool_from_config(value: Any, *, key: str) -> bool:
    """Parse top-level safety booleans without permissive truthiness."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    raise SystemExit(f"Refusing to run: {key} must be an explicit boolean true/false.")


def decimal_from_config(value: Any, default: Decimal) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def int_from_config(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(Decimal(str(value)))
    except Exception:
        return default


def parse_risk_filter_config(raw: Any) -> RiskFilterConfig:
    """Parse the deterministic option risk filter block with safe paper defaults."""
    defaults = RiskFilterConfig()
    if not isinstance(raw, dict):
        return defaults
    return RiskFilterConfig(
        enabled=bool_from_config(raw.get("enabled"), default=defaults.enabled),
        enforce_spread_filter=bool_from_config(raw.get("enforce_spread_filter"), default=defaults.enforce_spread_filter),
        max_spread_pct=decimal_from_config(raw.get("max_spread_pct"), defaults.max_spread_pct),
        max_spread_rupees=decimal_from_config(raw.get("max_spread_rupees"), defaults.max_spread_rupees),
        min_volume=int_from_config(raw.get("min_volume"), defaults.min_volume),
        min_oi=int_from_config(raw.get("min_oi"), defaults.min_oi),
        require_greeks=bool_from_config(raw.get("require_greeks"), default=defaults.require_greeks),
        min_abs_delta=decimal_from_config(raw.get("min_abs_delta"), defaults.min_abs_delta),
        max_abs_theta=decimal_from_config(raw.get("max_abs_theta"), defaults.max_abs_theta),
        max_iv=decimal_from_config(raw.get("max_iv"), defaults.max_iv),
        require_iv=bool_from_config(raw.get("require_iv"), default=defaults.require_iv),
    )


def _quote_metric_scopes(meta: Any) -> list[dict[str, Any]]:
    """Return candidate dicts to search, deepest/most-specific first.

    Supports both the nested FYERS raw shape (meta["raw"]["v"]) and a flat shape
    where bid/ask/spread/volume/greeks live directly on the quote metadata.
    """
    scopes: list[dict[str, Any]] = []
    if not isinstance(meta, dict):
        return scopes
    raw = meta.get("raw")
    if isinstance(raw, dict):
        v = raw.get("v")
        if isinstance(v, dict):
            scopes.append(v)
        scopes.append(raw)
    v_flat = meta.get("v")
    if isinstance(v_flat, dict):
        scopes.append(v_flat)
    scopes.append(meta)
    return scopes


def _first_decimal(scopes: list[dict[str, Any]], keys: tuple[str, ...]) -> Decimal | None:
    for scope in scopes:
        for key in keys:
            if key in scope and scope[key] is not None:
                try:
                    return Decimal(str(scope[key]))
                except Exception:
                    continue
    return None


def _first_int(scopes: list[dict[str, Any]], keys: tuple[str, ...]) -> int | None:
    value = _first_decimal(scopes, keys)
    return None if value is None else int(value)


def parse_option_quote_metrics(meta: Any, *, ltp: Decimal | None = None) -> OptionQuoteMetrics:
    """Robustly extract bid/ask/spread/volume and greeks from a FYERS quote.

    Pure parsing only — no network/DB/LLM. Spread is taken from an explicit
    spread field when present, otherwise derived from ask - bid. Spread percent
    is measured against the bid/ask mid when available, else against the LTP.
    """
    scopes = _quote_metric_scopes(meta)
    bid = _first_decimal(scopes, ("bid", "bid_price", "bp"))
    ask = _first_decimal(scopes, ("ask", "ask_price", "ap"))
    explicit_spread = _first_decimal(scopes, ("spread", "bid_ask_spread"))
    volume = _first_int(scopes, ("volume", "vol", "tot_traded_qty", "v"))
    oi = _first_int(scopes, ("oi", "open_interest", "openInterest"))
    delta = _first_decimal(scopes, ("delta",))
    gamma = _first_decimal(scopes, ("gamma",))
    theta = _first_decimal(scopes, ("theta",))
    vega = _first_decimal(scopes, ("vega",))
    iv = _first_decimal(scopes, ("iv", "implied_volatility", "imp_volatility"))

    spread = explicit_spread
    if spread is None and bid is not None and ask is not None:
        spread = ask - bid

    reference: Decimal | None = None
    if bid is not None and ask is not None and (bid + ask) > 0:
        reference = (bid + ask) / Decimal("2")
    elif ltp is not None and ltp > 0:
        reference = ltp

    spread_pct: Decimal | None = None
    if spread is not None and reference is not None and reference > 0:
        spread_pct = (spread / reference * Decimal("100")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    return OptionQuoteMetrics(
        bid=bid,
        ask=ask,
        spread=spread,
        spread_pct=spread_pct,
        volume=volume,
        oi=oi,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        iv=iv,
    )


# Each _check_* helper below is a pure, side-effect-free audit of one risk
# dimension. It returns (reasons, warnings): a non-empty reasons list blocks the
# trade; warnings are advisory only (typically a metric we do not yet ingest).
# evaluate_option_risk_filters runs them in a fixed order so the rejection
# message recorded on the paper trade is deterministic.


def _check_spread(metrics: OptionQuoteMetrics, rf: RiskFilterConfig) -> tuple[list[str], list[str]]:
    """Bid/ask spread guard: reject when the spread is wider than the configured
    percent and/or rupee caps (each disabled when its threshold is 0)."""
    reasons: list[str] = []
    warnings: list[str] = []
    if not rf.enforce_spread_filter:
        return reasons, warnings
    if metrics.spread is None:
        reasons.append("spread unavailable; bid/ask not present in quote")
        return reasons, warnings
    if rf.max_spread_pct > 0:
        if metrics.spread_pct is None:
            warnings.append("spread percent unavailable; no reference price")
        elif metrics.spread_pct > rf.max_spread_pct:
            reasons.append(f"spread {metrics.spread_pct}% exceeds max {rf.max_spread_pct}%")
    if rf.max_spread_rupees > 0 and metrics.spread > rf.max_spread_rupees:
        reasons.append(f"spread ₹{metrics.spread} exceeds max ₹{rf.max_spread_rupees}")
    return reasons, warnings


def _check_volume(metrics: OptionQuoteMetrics, rf: RiskFilterConfig) -> tuple[list[str], list[str]]:
    """Liquidity guard: reject when traded volume is below the configured floor
    (disabled when min_volume is 0)."""
    reasons: list[str] = []
    warnings: list[str] = []
    if rf.min_volume > 0:
        if metrics.volume is None:
            reasons.append("volume unavailable in quote")
        elif metrics.volume < rf.min_volume:
            reasons.append(f"volume {metrics.volume} below min {rf.min_volume}")
    return reasons, warnings


def _check_open_interest(metrics: OptionQuoteMetrics, rf: RiskFilterConfig) -> tuple[list[str], list[str]]:
    """Open-interest liquidity guard, mirroring _check_volume's fail-closed policy:
    once min_oi is configured (> 0), a missing OI blocks the trade rather than
    silently passing. OI is sourced from the option-chain snapshot. Disabled when
    min_oi is 0, so today's behavior is unchanged until the operator opts in."""
    reasons: list[str] = []
    warnings: list[str] = []
    if rf.min_oi > 0:
        if metrics.oi is None:
            reasons.append("open interest unavailable in quote/chain")
        elif metrics.oi < rf.min_oi:
            reasons.append(f"open interest {metrics.oi} below min {rf.min_oi}")
    return reasons, warnings


def _check_greeks(metrics: OptionQuoteMetrics, rf: RiskFilterConfig) -> tuple[list[str], list[str]]:
    """Delta/theta guard. Missing greeks block only when require_greeks is set,
    otherwise they are advisory so the filter never blocks on data we do not yet
    ingest. Delta floor and theta cap are each disabled when their threshold is 0."""
    reasons: list[str] = []
    warnings: list[str] = []
    if metrics.delta is None:
        msg = "delta/greeks unavailable in quote"
        (reasons if (rf.require_greeks or rf.min_abs_delta > 0) else warnings).append(msg)
    elif rf.min_abs_delta > 0 and abs(metrics.delta) < rf.min_abs_delta:
        reasons.append(f"abs(delta) {abs(metrics.delta)} below min {rf.min_abs_delta}")

    if rf.max_abs_theta > 0:
        if metrics.theta is None:
            reasons.append("theta unavailable; max_abs_theta cap not applied")
        elif abs(metrics.theta) > rf.max_abs_theta:
            reasons.append(f"abs(theta) {abs(metrics.theta)} exceeds max {rf.max_abs_theta}")
    return reasons, warnings


def _check_iv(metrics: OptionQuoteMetrics, rf: RiskFilterConfig) -> tuple[list[str], list[str]]:
    """Implied-volatility regime guard. Missing IV blocks only when require_iv is
    set; the IV ceiling is disabled when max_iv is 0."""
    reasons: list[str] = []
    warnings: list[str] = []
    if metrics.iv is None:
        if rf.require_iv:
            reasons.append("implied volatility unavailable but required")
        elif rf.max_iv > 0:
            reasons.append("implied volatility unavailable; max_iv cap not applied")
    elif rf.max_iv > 0 and metrics.iv > rf.max_iv:
        reasons.append(f"iv {metrics.iv} exceeds max {rf.max_iv}")
    return reasons, warnings


# Ordered so the recorded rejection reasons are deterministic: spread, then
# volume/open-interest liquidity, then greeks, then IV regime.
_RISK_FILTER_CHECKS = (_check_spread, _check_volume, _check_open_interest, _check_greeks, _check_iv)


def evaluate_option_risk_filters(
    *,
    option_ltp: Decimal,
    option_meta: dict[str, Any],
    option_type: str,
    risk_filter: RiskFilterConfig,
) -> RiskFilterDecision:
    """Pure deterministic option risk gate. No network/DB/LLM calls.

    Returns a decision with allowed flag, rejection reasons, non-blocking
    warnings (e.g. metric unavailable), and the raw metrics/thresholds used so
    the decision is fully auditable in the paper-trade record.
    """
    metrics = parse_option_quote_metrics(option_meta, ltp=option_ltp)
    reasons: list[str] = []
    warnings: list[str] = []

    raw: dict[str, Any] = {
        "enabled": risk_filter.enabled,
        "option_type": option_type,
        "option_ltp": str(option_ltp),
        **metrics.as_raw(),
        "thresholds": {
            "enforce_spread_filter": risk_filter.enforce_spread_filter,
            "max_spread_pct": str(risk_filter.max_spread_pct),
            "max_spread_rupees": str(risk_filter.max_spread_rupees),
            "min_volume": risk_filter.min_volume,
            "min_oi": risk_filter.min_oi,
            "require_greeks": risk_filter.require_greeks,
            "min_abs_delta": str(risk_filter.min_abs_delta),
            "max_abs_theta": str(risk_filter.max_abs_theta),
            "max_iv": str(risk_filter.max_iv),
            "require_iv": risk_filter.require_iv,
        },
    }

    if not risk_filter.enabled:
        return RiskFilterDecision(allowed=True, reasons=[], warnings=[], raw=raw)

    for check in _RISK_FILTER_CHECKS:
        check_reasons, check_warnings = check(metrics, risk_filter)
        reasons.extend(check_reasons)
        warnings.extend(check_warnings)

    return RiskFilterDecision(
        allowed=not reasons,
        reasons=reasons,
        warnings=warnings,
        raw=raw,
    )


def parse_chain_signal_config(raw: Any) -> ChainSignalConfig:
    """Parse the option-chain signal-gate block with safe advisory defaults."""
    defaults = ChainSignalConfig()
    if not isinstance(raw, dict):
        return defaults
    return ChainSignalConfig(
        enabled=bool_from_config(raw.get("enabled"), default=defaults.enabled),
        block_on_iv_regime_high=bool_from_config(raw.get("block_on_iv_regime_high"), default=defaults.block_on_iv_regime_high),
        block_on_contradicting_oi=bool_from_config(raw.get("block_on_contradicting_oi"), default=defaults.block_on_contradicting_oi),
        block_on_pcr_extreme=bool_from_config(raw.get("block_on_pcr_extreme"), default=defaults.block_on_pcr_extreme),
        pcr_bullish_max=decimal_from_config(raw.get("pcr_bullish_max"), defaults.pcr_bullish_max),
        pcr_bearish_min=decimal_from_config(raw.get("pcr_bearish_min"), defaults.pcr_bearish_min),
    )


def evaluate_chain_signals(*, direction: str, summary: dict[str, Any], cfg: ChainSignalConfig) -> ChainSignalDecision:
    """Pure deterministic option-chain context gate for a long CE/PE entry.

    No network/DB. Reads the latest chain summary (PCR, ATM IV regime, OI-buildup
    label) and flags context that contradicts the trade direction. Each contradiction
    is blocking only when its block_* flag is set, otherwise advisory (recorded as a
    warning). Long options dislike a 'high' IV regime regardless of direction. A
    'call_buildup' (call writing) contradicts a long CE; 'put_buildup' contradicts a
    long PE. PCR skew is checked against the configured per-direction bounds."""
    reasons: list[str] = []
    warnings: list[str] = []
    pcr = summary.get("pcr")
    iv_regime = summary.get("iv_regime")
    oi_label = summary.get("oi_buildup_label")
    raw = {
        "enabled": cfg.enabled,
        "direction": direction,
        "pcr": None if pcr is None else str(pcr),
        "iv_regime": iv_regime,
        "oi_buildup_label": oi_label,
        "thresholds": {
            "block_on_iv_regime_high": cfg.block_on_iv_regime_high,
            "block_on_contradicting_oi": cfg.block_on_contradicting_oi,
            "block_on_pcr_extreme": cfg.block_on_pcr_extreme,
            "pcr_bullish_max": str(cfg.pcr_bullish_max),
            "pcr_bearish_min": str(cfg.pcr_bearish_min),
        },
    }
    if not cfg.enabled:
        return ChainSignalDecision(allowed=True, reasons=[], warnings=[], raw=raw)

    def route(blocking: bool, message: str) -> None:
        (reasons if blocking else warnings).append(message)

    if iv_regime == "high":
        route(cfg.block_on_iv_regime_high, "ATM IV regime high — long-option premium expensive")

    if direction == "CE" and oi_label == "call_buildup":
        route(cfg.block_on_contradicting_oi, "call OI buildup (call writing) contradicts long CE")
    elif direction == "PE" and oi_label == "put_buildup":
        route(cfg.block_on_contradicting_oi, "put OI buildup (put writing) contradicts long PE")

    if pcr is not None:
        if direction == "CE" and cfg.pcr_bullish_max > 0 and pcr > cfg.pcr_bullish_max:
            route(cfg.block_on_pcr_extreme, f"PCR {pcr} above {cfg.pcr_bullish_max} — heavy put writing/bearish skew vs long CE")
        elif direction == "PE" and cfg.pcr_bearish_min > 0 and pcr < cfg.pcr_bearish_min:
            route(cfg.block_on_pcr_extreme, f"PCR {pcr} below {cfg.pcr_bearish_min} — heavy call writing/bullish skew vs long PE")

    return ChainSignalDecision(allowed=not reasons, reasons=reasons, warnings=warnings, raw=raw)


def default_strategy_router() -> tuple[StrategyCardRule, ...]:
    return (
        StrategyCardRule(
            strategy_id="banknifty_constituent_led_directional_long_options",
            name="BankNifty Constituent-Led Directional Long Options",
            enabled=True,
            paper_trade_enabled=True,
            entry_function="constituent_led_long_options",
            status="paper_ready",
            source="Trading Vault/03 Strategy Ideas/BankNifty Constituent-Led Directional Long Options.md",
            notes="Only active paper entry strategy. Long CE/PE only; no live orders.",
        ),
        StrategyCardRule(
            strategy_id="banknifty_official_payoff_structure_selector",
            name="BankNifty Official Payoff Structure Selector",
            enabled=False,
            paper_trade_enabled=False,
            entry_function="not_implemented",
            status="research_only",
            source="Trading Vault/03 Strategy Ideas/BankNifty Official Payoff Structure Selector.md",
            notes="Payoff routing not implemented; short-premium structures remain blocked.",
        ),
        StrategyCardRule(
            strategy_id="options_360_short_straddle_strangle_premium_decay",
            name="Options 360 Short Straddle/Strangle Premium Decay",
            enabled=False,
            paper_trade_enabled=False,
            entry_function="research_only",
            status="research_only_blocked",
            source="Trading Vault/03 Strategy Ideas/Options 360 Short Straddle Strangle Premium Decay.md",
            notes="Undefined-risk option selling is blocked until margin, gap, adjustment, and slippage are modeled.",
        ),
        StrategyCardRule(
            strategy_id="options_greeks_risk_filter",
            name="Options Greeks Risk Filter for Index Options",
            enabled=False,
            paper_trade_enabled=False,
            entry_function="options_greeks_risk_filter",
            status="filter_active",
            source="Trading Vault/03 Strategy Ideas/Options Greeks Risk Filter for Index Options.md",
            notes="Guardrail filter applied pre-entry via risk_filter config; never a runnable entry strategy.",
            card_type="filter",
        ),
        StrategyCardRule(
            strategy_id="implied_volatility_regime_filter",
            name="Implied Volatility Regime Filter for Long Options",
            enabled=False,
            paper_trade_enabled=False,
            entry_function="implied_volatility_regime_filter",
            status="filter_active",
            source="Trading Vault/03 Strategy Ideas/Implied Volatility Regime Filter for Long Options.md",
            notes="Guardrail filter applied pre-entry via risk_filter config; never a runnable entry strategy.",
            card_type="filter",
        ),
        StrategyCardRule(
            strategy_id="trading_psychology_execution_guardrails",
            name="Trading Psychology Execution Guardrails",
            enabled=False,
            paper_trade_enabled=False,
            entry_function="guardrail_partially_implemented",
            status="guardrail_partial",
            source="Trading Vault/03 Strategy Ideas/Trading Psychology Execution Guardrails.md",
            notes="Daily loss, max trades, max open positions, and paper-only guardrails are enforced centrally.",
            card_type="guardrail",
        ),
    )


def parse_strategy_router(raw: Any) -> tuple[StrategyCardRule, ...]:
    if not isinstance(raw, list) or not raw:
        return default_strategy_router()
    cards: list[StrategyCardRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        strategy_id = str(item.get("id") or item.get("strategy_id") or "").strip()
        if not strategy_id:
            continue
        cards.append(
            StrategyCardRule(
                strategy_id=strategy_id,
                name=str(item.get("name") or strategy_id.replace("_", " ").title()).strip(),
                enabled=bool_from_config(item.get("enabled"), default=False),
                paper_trade_enabled=bool_from_config(item.get("paper_trade_enabled"), default=False),
                entry_function=str(item.get("entry_function") or "not_implemented").strip(),
                status=str(item.get("status") or "research_only").strip(),
                source=str(item.get("source") or "").strip(),
                notes=str(item.get("notes") or "").strip(),
                card_type=str(item.get("card_type") or item.get("kind") or item.get("role") or "unspecified").strip().lower(),
            )
        )
    return tuple(cards) if cards else default_strategy_router()


def runnable_entry_strategy_cards(cards: tuple[StrategyCardRule, ...]) -> tuple[StrategyCardRule, ...]:
    """Return only cards that may place a paper entry.

    A card must be explicitly card_type="entry"; filter and guardrail cards are
    excluded here by construction, so they can never become runnable entries
    even if their enabled/paper_trade flags are flipped on.
    """
    return tuple(
        card
        for card in cards
        if card.card_type == "entry"
        and card.enabled
        and card.paper_trade_enabled
        and card.entry_function not in {"", "not_implemented", "research_only", "risk_filter_not_implemented"}
    )


def filter_cards(cards: tuple[StrategyCardRule, ...]) -> tuple[StrategyCardRule, ...]:
    """Return pre-entry risk/IV filter cards — never runnable entry strategies."""
    return tuple(card for card in cards if card.card_type == "filter")


def guardrail_cards(cards: tuple[StrategyCardRule, ...]) -> tuple[StrategyCardRule, ...]:
    """Return execution/guardrail cards — never runnable entry strategies."""
    return tuple(card for card in cards if card.card_type == "guardrail")


def risk_filter_summary_line(decision: RiskFilterDecision | None) -> str | None:
    """One concise, auditable line describing the option risk-filter outcome."""
    if decision is None:
        return None
    raw = decision.raw
    spread_pct = raw.get("spread_pct")
    spread = raw.get("spread")
    volume = raw.get("volume")
    parts = [
        f"spread {spread_pct}%" if spread_pct is not None else "spread n/a%",
        f"₹{spread}" if spread is not None else "₹n/a",
        f"vol {volume}" if volume is not None else "vol n/a",
    ]
    status = "pass" if decision.allowed else "reject"
    line = f"Risk filter: {status} | {' / '.join(parts)}"
    if decision.warnings:
        line += f" | warnings: {'; '.join(decision.warnings)}"
    return line


def config_get(data: dict[str, Any], dotted_key: str, default: Any) -> Any:
    """Read either a legacy top-level key or a nested spec key from JSON config."""
    if dotted_key in data:
        return data[dotted_key]
    current: Any = data
    parts = dotted_key.split(".")
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            legacy_key = parts[-1]
            return data.get(legacy_key, default)
        current = current[part]
    return current


def selected_constituent_led_strategy(config: CampaignConfig) -> StrategyCardRule | None:
    for card in runnable_entry_strategy_cards(config.strategy_router):
        if card.entry_function == "constituent_led_long_options":
            return card
    return None


def strategy_card_as_raw(card: StrategyCardRule) -> dict[str, Any]:
    return {
        "id": card.strategy_id,
        "name": card.name,
        "enabled": card.enabled,
        "paper_trade_enabled": card.paper_trade_enabled,
        "entry_function": card.entry_function,
        "status": card.status,
        "source": card.source,
        "notes": card.notes,
        "card_type": card.card_type,
    }


def load_config(path: Path = DEFAULT_CONFIG) -> CampaignConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not strict_bool_from_config(data.get("paper_only", True), key="paper_only"):
        raise SystemExit("Refusing to run: paper_only must be true.")
    if strict_bool_from_config(data.get("live_orders_enabled", False), key="live_orders_enabled"):
        raise SystemExit("Refusing to run: live_orders_enabled must be false.")

    max_daily_loss = Decimal(str(data.get("max_daily_loss", 500)))
    max_trades_per_day = int(data.get("max_trades_per_day", 2))
    max_trade_loss = Decimal(str(data.get("max_trade_loss", 0)))
    worst_case_daily_trade_loss = Decimal(max_trades_per_day) * max_trade_loss
    if max_trade_loss > 0 and worst_case_daily_trade_loss > max_daily_loss:
        raise SystemExit(
            "Refusing to run: max_trades_per_day * max_trade_loss "
            f"({max_trades_per_day} * {max_trade_loss} = {worst_case_daily_trade_loss}) "
            f"exceeds max_daily_loss ({max_daily_loss})."
        )

    # The engine reads the top-level risk keys only. Refuse to run when the
    # documentation-style nested "risk" block disagrees, so the two can't drift.
    nested_risk = data.get("risk") if isinstance(data.get("risk"), dict) else {}
    nested_risk_pairs = (
        ("max_trade_loss_inr", max_trade_loss),
        ("max_daily_loss_inr", max_daily_loss),
        ("max_trades_per_day", Decimal(max_trades_per_day)),
        ("max_open_positions", Decimal(str(data.get("max_open_positions", 1)))),
        ("max_premium_exposure_inr", Decimal(str(data.get("max_premium_exposure", 1500)))),
    )
    for nested_key, top_value in nested_risk_pairs:
        if nested_key in nested_risk and Decimal(str(nested_risk[nested_key])) != top_value:
            raise SystemExit(
                f"Refusing to run: config risk.{nested_key} ({nested_risk[nested_key]}) "
                f"disagrees with the enforced top-level value ({top_value})."
            )

    return CampaignConfig(
        campaign_name=str(data.get("campaign_name", "banknifty_options_paper_50000_2026-06-08")),
        strategy_version=str(data.get("strategy_version", "banknifty_options_paper_v1")),
        underlying=str(data.get("underlying", "BANKNIFTY")),
        underlying_symbol=str(data.get("underlying_symbol", "NSE:NIFTYBANK-INDEX")),
        starting_capital=Decimal(str(data.get("starting_capital", 5000))),
        max_premium_exposure=Decimal(str(data.get("max_premium_exposure", 1500))),
        max_daily_loss=max_daily_loss,
        max_open_positions=int(data.get("max_open_positions", 1)),
        max_trades_per_day=max_trades_per_day,
        max_trade_loss=max_trade_loss,
        stop_loss_pct=Decimal(str(data.get("stop_loss_pct", 0.08))),
        target_pct=Decimal(str(data.get("target_pct", 0.06))),
        fixed_target_exit_enabled=bool_from_config(data.get("fixed_target_exit_enabled"), default=True),
        realistic_risk_enabled=bool_from_config(data.get("realistic_risk_enabled"), default=True),
        structure_candle_resolution=str(data.get("structure_candle_resolution", "5")),
        option_structure_lookback_candles=int(data.get("option_structure_lookback_candles", 6)),
        atr_buffer_multiplier=Decimal(str(data.get("atr_buffer_multiplier", 0.20))),
        target_r_multiple=Decimal(str(data.get("target_r_multiple", 1.20))),
        profit_lock_trigger=Decimal(str(data.get("profit_lock_trigger", 0))),
        profit_lock_step=Decimal(str(data.get("profit_lock_step", 0))),
        option_tick_size=Decimal(str(data.get("option_tick_size", 0.05))),
        strike_step=Decimal(str(data.get("strike_step", 100))),
        signal_threshold_pct=Decimal(str(data.get("signal_threshold_pct", 0.10))),
        min_index_confirmation_pct=Decimal(str(data.get("min_index_confirmation_pct", 0.05))),
        min_constituent_coverage_pct=Decimal(str(data.get("min_constituent_coverage_pct", 70))),
        min_directional_weight_pct=Decimal(str(data.get("min_directional_weight_pct", 60))),
        index_structure_confirmation_enabled=bool_from_config(data.get("index_structure_confirmation_enabled"), default=True),
        index_structure_lookback_candles=int(data.get("index_structure_lookback_candles", 8)),
        index_structure_breakout_buffer_pct=Decimal(str(data.get("index_structure_breakout_buffer_pct", 0.02))),
        index_structure_stop_buffer_pct=Decimal(str(data.get("index_structure_stop_buffer_pct", 0.03))),
        swing_trailing_enabled=bool_from_config(data.get("swing_trailing_enabled"), default=True),
        vwap_volume_confirmation_enabled=bool_from_config(data.get("vwap_volume_confirmation_enabled"), default=True),
        min_vwap_volume_confirming_top_movers=int(config_get(data, "direction_layer.min_vwap_volume_confirming_top_movers", data.get("min_vwap_volume_confirming_top_movers", 1))),
        relative_volume_threshold=Decimal(str(config_get(data, "direction_layer.rel_volume_threshold", data.get("relative_volume_threshold", 1.20)))),
        major_jump_threshold_pct=Decimal(str(data.get("major_jump_threshold_pct", 1.50))),
        weighted_vwap_side_pct=Decimal(str(config_get(data, "direction_layer.weighted_vwap_side_pct", data.get("weighted_vwap_side_pct", 60)))),
        no_new_trades_before=parse_time(str(config_get(data, "filters.no_entry_before", data.get("no_new_trades_before", "09:35")))),
        lunch_window_start=parse_time(str((config_get(data, "filters.lunch_window", ["11:30", "13:15"]) or ["11:30", "13:15"])[0])),
        lunch_window_end=parse_time(str((config_get(data, "filters.lunch_window", ["11:30", "13:15"]) or ["11:30", "13:15"])[1])),
        lunch_min_day_range_vs_adr10=Decimal(str(config_get(data, "filters.lunch_min_day_range_vs_adr10", 0.6))),
        lunch_min_relvol=Decimal(str(config_get(data, "filters.lunch_min_relvol", 1.3))),
        expiry_pm_min_relvol=Decimal(str(config_get(data, "filters.expiry_pm_min_relvol", 1.5))),
        chop_lookback_candles=int(config_get(data, "filters.chop_lookback_candles", 12)),
        chop_max_net_move_pct=Decimal(str(config_get(data, "filters.chop_max_net_move_pct", 0.15))),
        chop_max_vwap_crosses=int(config_get(data, "filters.chop_max_vwap_crosses", 3)),
        leg_lookback_candles=int(config_get(data, "trend_leg.leg_lookback_candles", data.get("leg_lookback_candles", 6))),
        confluence_levels=tuple(config_get(data, "trend_leg.confluence_levels", ["orb", "pdh_pdl", "structure_8"])),
        pullback_max_candles=int(config_get(data, "pullback.max_pullback_candles", data.get("pullback_max_candles", 4))),
        pullback_level_hold_buffer_pct=Decimal(str(config_get(data, "pullback.level_hold_buffer_pct", data.get("pullback_level_hold_buffer_pct", 0.02)))),
        beta_lookback_min=int(config_get(data, "stops.beta_lookback_min", 30)),
        beta_fallback_atm=Decimal(str(config_get(data, "stops.beta_fallback_atm", 0.5))),
        beta_fallback_otm1=Decimal(str(config_get(data, "stops.beta_fallback_otm1", 0.35))),
        breakeven_at_r=Decimal(str(config_get(data, "exits.breakeven_at_r", 0.8))),
        ratchet_start_r=Decimal(str(config_get(data, "exits.ratchet_start_r", 1.0))),
        ratchet_giveback_pct=Decimal(str(config_get(data, "exits.ratchet_giveback_pct", 35))),
        ratchet_giveback_min_inr=Decimal(str(config_get(data, "exits.ratchet_giveback_min_inr", 600))),
        stagnation_minutes=int(config_get(data, "exits.stagnation_minutes", 30)),
        stagnation_min_r=Decimal(str(config_get(data, "exits.stagnation_min_r", 0.3))),
        constituents=parse_constituents(data.get("constituents")),
        no_new_trades_after=parse_time(str(data.get("no_new_trades_after", "14:45"))),
        force_exit_time=parse_time(str(data.get("force_exit_time", "15:20"))),
        entry_scan_interval_minutes=int(data.get("entry_scan_interval_minutes", 5)),
        open_position_update_interval_minutes=int(data.get("open_position_update_interval_minutes", 5)),
        poll_interval_seconds=int(data.get("poll_interval_seconds", 15)),
        quote_stale_seconds=int(data.get("quote_stale_seconds", 90)),
        chain_stale_seconds=int(data.get("chain_stale_seconds", 180)),
        chain_selection_enabled=bool_from_config(data.get("chain_selection_enabled"), default=False),
        paper_only=True,
        live_orders_enabled=False,
        notes=str(data.get("notes", "BankNifty options paper campaign; no live orders.")),
        strategy_router=parse_strategy_router(data.get("strategy_router")),
        risk_filter=parse_risk_filter_config(data.get("risk_filter")),
        chain_signals=parse_chain_signal_config(data.get("chain_signals")),
    )


def parse_fyers_option_row(row: list[str]) -> FyersOptionContract | None:
    """Parse one FYERS NSE_FO.csv row for BankNifty option contracts.

    Known row shape from FYERS public master:
    token, description, instrument_type, lot_size, tick_size, ..., expiry_epoch,
    fyers_symbol, ..., strike, option_type, ...
    """
    if len(row) < 17:
        return None
    underlying = row[13].strip().upper()
    option_type = row[16].strip().upper()
    if underlying != "BANKNIFTY" or option_type not in {"CE", "PE"}:
        return None
    symbol = row[9].strip()
    if not symbol.startswith("NSE:BANKNIFTY"):
        return None
    try:
        expiry = datetime.fromtimestamp(int(row[8]), tz=timezone.utc).date()
        strike = Decimal(row[15])
        lot_size = int(Decimal(row[3]))
        tick_size = Decimal(row[4])
    except Exception:
        return None
    return FyersOptionContract(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        lot_size=lot_size,
        tick_size=tick_size,
        raw={"row": row},
    )


def fetch_fyers_banknifty_options() -> list[FyersOptionContract]:
    with urllib.request.urlopen(FYERS_NSE_FO_MASTER_URL, timeout=60) as response:
        text = response.read().decode("utf-8", "replace")
    contracts: list[FyersOptionContract] = []
    for row in csv.reader(io.StringIO(text)):
        contract = parse_fyers_option_row(row)
        if contract:
            contracts.append(contract)
    return contracts


def nearest_strike(underlying_ltp: Decimal, step: Decimal) -> Decimal:
    return (underlying_ltp / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def select_atm_contracts(
    contracts: list[FyersOptionContract],
    *,
    underlying_ltp: Decimal,
    today: date,
    strike_step: Decimal = Decimal("100"),
) -> dict[str, FyersOptionContract]:
    future_expiries = sorted({c.expiry for c in contracts if c.expiry >= today})
    if not future_expiries:
        raise ValueError("No non-expired BankNifty option expiries found in FYERS master.")
    expiry = future_expiries[0]
    atm = nearest_strike(underlying_ltp, strike_step)
    selected: dict[str, FyersOptionContract] = {}
    for option_type in ("CE", "PE"):
        candidates = [c for c in contracts if c.expiry == expiry and c.option_type == option_type]
        if not candidates:
            continue
        selected[option_type] = min(candidates, key=lambda c: (abs(c.strike - atm), c.symbol))
    if set(selected) != {"CE", "PE"}:
        raise ValueError(f"Could not find both ATM CE/PE for expiry {expiry} around {atm}.")
    return selected


def select_directional_contract_candidates(
    contracts: list[FyersOptionContract],
    *,
    direction: str,
    underlying_ltp: Decimal,
    today: date,
    strike_step: Decimal = Decimal("100"),
) -> list[FyersOptionContract]:
    """Return ATM then first OTM only; never ITM/deeper OTM for this strategy."""
    future_expiries = sorted({c.expiry for c in contracts if c.expiry >= today})
    if not future_expiries:
        return []
    expiry = future_expiries[0]
    atm = nearest_strike(underlying_ltp, strike_step)
    otm = atm + strike_step if direction == "CE" else atm - strike_step
    wanted = [atm, otm]
    result: list[FyersOptionContract] = []
    for strike in wanted:
        matches = [c for c in contracts if c.expiry == expiry and c.option_type == direction and c.strike == strike]
        if matches:
            result.append(sorted(matches, key=lambda c: c.symbol)[0])
    return result


def rank_chain_candidates(
    candidates: list[FyersOptionContract],
    metrics_by_symbol: dict[str, dict[str, Any]],
) -> list[FyersOptionContract]:
    """Reorder ATM/OTM candidates by option-chain liquidity, most tradable first.

    Pure and deterministic. Candidates with chain metrics sort ahead of those
    without; among them the key prefers higher open interest then a tighter
    bid/ask spread. The original index is the final tiebreak so the sort is stable
    and ATM-first ordering is preserved on ties. When no candidate has metrics the
    input order is returned unchanged (today's behavior)."""
    if not any(metrics_by_symbol.get(c.symbol) for c in candidates):
        return list(candidates)

    def sort_key(item: tuple[int, FyersOptionContract]) -> tuple[int, Decimal, Decimal, int]:
        idx, c = item
        m = metrics_by_symbol.get(c.symbol)
        if not m:
            return (1, Decimal("0"), Decimal("0"), idx)
        oi = m.get("oi")
        oi_rank = Decimal(-int(oi)) if oi is not None else Decimal("0")  # higher OI first
        bid, ask = m.get("bid"), m.get("ask")
        if bid is not None and ask is not None:
            spread = Decimal(str(ask)) - Decimal(str(bid))
        else:
            spread = Decimal("9999")  # unknown spread sinks below known-tight ones
        return (0, oi_rank, spread, idx)

    return [c for _, c in sorted(enumerate(candidates), key=sort_key)]


def round_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= 0:
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    ticks = (value / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (ticks * tick_size).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def floor_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= 0:
        return value.quantize(TWO_PLACES, rounding=ROUND_DOWN)
    ticks = (value / tick_size).to_integral_value(rounding=ROUND_DOWN)
    return (ticks * tick_size).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def size_option_lots(option_premium: Decimal, *, lot_size: int, max_premium_exposure: Decimal) -> tuple[int, int, Decimal]:
    if option_premium <= 0 or lot_size <= 0 or max_premium_exposure <= 0:
        return 0, 0, Decimal("0.00")
    one_lot_value = option_premium * Decimal(lot_size)
    lots = int((max_premium_exposure / one_lot_value).to_integral_value(rounding=ROUND_DOWN))
    if lots < 1:
        return 0, 0, Decimal("0.00")
    quantity = lots * lot_size
    premium_value = (option_premium * Decimal(quantity)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return lots, quantity, premium_value


def size_lots_by_risk(
    *,
    entry_premium: Decimal,
    estimated_stop_premium: Decimal,
    lot_size: int,
    max_trade_loss: Decimal,
    max_premium_exposure: Decimal,
) -> tuple[int, int, Decimal, Decimal]:
    if entry_premium <= 0 or estimated_stop_premium <= 0 or estimated_stop_premium >= entry_premium or lot_size <= 0:
        return 0, 0, Decimal("0.00"), Decimal("0.00")
    risk_per_lot = (entry_premium - estimated_stop_premium) * Decimal(lot_size)
    if risk_per_lot <= 0 or risk_per_lot > max_trade_loss:
        return 0, 0, Decimal("0.00"), Decimal("0.00")
    lots_by_risk = int((max_trade_loss / risk_per_lot).to_integral_value(rounding=ROUND_DOWN))
    one_lot_exposure = entry_premium * Decimal(lot_size)
    lots_by_exposure = int((max_premium_exposure / one_lot_exposure).to_integral_value(rounding=ROUND_DOWN)) if one_lot_exposure > 0 else 0
    lots = min(lots_by_risk, lots_by_exposure)
    if lots < 1:
        return 0, 0, Decimal("0.00"), Decimal("0.00")
    quantity = lots * lot_size
    exposure = (entry_premium * Decimal(quantity)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    risk = ((entry_premium - estimated_stop_premium) * Decimal(quantity)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return lots, quantity, exposure, risk


def build_stop_target(
    entry_premium: Decimal,
    *,
    stop_loss_pct: Decimal,
    target_pct: Decimal,
    tick_size: Decimal,
) -> tuple[Decimal, Decimal]:
    stop = round_to_tick(entry_premium * (Decimal("1") - stop_loss_pct), tick_size)
    target = round_to_tick(entry_premium * (Decimal("1") + target_pct), tick_size)
    return stop, target


def cap_stop_by_trade_loss(entry_premium: Decimal, stop_premium: Decimal, quantity: int, max_trade_loss: Decimal, tick_size: Decimal) -> Decimal:
    """Tighten the initial stop so one paper trade cannot exceed the rupee risk cap."""
    if max_trade_loss <= 0 or quantity <= 0:
        return stop_premium
    capped_stop = floor_to_tick(entry_premium - (max_trade_loss / Decimal(quantity)), tick_size)
    if capped_stop >= entry_premium:
        capped_stop = floor_to_tick(entry_premium - tick_size, tick_size)
    return max(stop_premium, capped_stop)



def candle_value(candle: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(candle[key]))


def candle_true_ranges(candles: list[dict[str, Any]]) -> list[Decimal]:
    ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for candle in candles:
        high = candle_value(candle, "high")
        low = candle_value(candle, "low")
        if previous_close is None:
            ranges.append(high - low)
        else:
            ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = candle_value(candle, "close")
    return ranges


def average_true_range(candles: list[dict[str, Any]], *, period: int | None = None) -> Decimal | None:
    if not candles:
        return None
    ranges = candle_true_ranges(candles)
    if period is not None and period > 0:
        ranges = ranges[-period:]
    if not ranges:
        return None
    return (sum(ranges, Decimal("0")) / Decimal(len(ranges))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def estimate_option_index_slope(index_candles: list[dict[str, Any]], option_candles: list[dict[str, Any]]) -> Decimal | None:
    if len(index_candles) < 2 or len(option_candles) < 2:
        return None
    if all("ts" in c for c in index_candles) and all("ts" in c for c in option_candles):
        index_by_ts = {c["ts"]: c for c in index_candles}
        paired = [(index_by_ts[c["ts"]], c) for c in option_candles if c.get("ts") in index_by_ts]
    else:
        count = min(len(index_candles), len(option_candles))
        paired = list(zip(index_candles[-count:], option_candles[-count:]))
    slopes: list[Decimal] = []
    for (prev_index, prev_option), (cur_index, cur_option) in zip(paired, paired[1:]):
        index_delta = candle_value(cur_index, "close") - candle_value(prev_index, "close")
        option_delta = candle_value(cur_option, "close") - candle_value(prev_option, "close")
        if index_delta != 0:
            slope = abs(option_delta / index_delta)
            if slope > 0:
                slopes.append(slope)
    if not slopes:
        return None
    return (sum(slopes, Decimal("0")) / Decimal(len(slopes))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def structure_stop_from_index_candles(
    *,
    option_type: str,
    index_candles: list[dict[str, Any]],
    atr_buffer_multiplier: Decimal,
) -> Decimal | None:
    if not index_candles:
        return None
    atr = average_true_range(index_candles)
    buffer = (atr or Decimal("0")) * atr_buffer_multiplier
    if option_type == "CE":
        base = min(candle_value(candle, "low") for candle in index_candles)
        return (base - buffer).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    base = max(candle_value(candle, "high") for candle in index_candles)
    return (base + buffer).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def option_structure_stop_from_candles(
    *,
    option_candles: list[dict[str, Any]],
    atr_buffer_multiplier: Decimal,
    tick_size: Decimal,
) -> Decimal | None:
    if not option_candles:
        return None
    atr = average_true_range(option_candles)
    buffer = (atr or Decimal("0")) * atr_buffer_multiplier
    base = min(candle_value(candle, "low") for candle in option_candles)
    return floor_to_tick(base - buffer, tick_size)


def build_realistic_stop_target(
    *,
    entry_premium: Decimal,
    option_ltp: Decimal,
    index_ltp: Decimal | None,
    option_type: str,
    quantity: int,
    max_trade_loss: Decimal,
    tick_size: Decimal,
    option_candles: list[dict[str, Any]],
    index_candles: list[dict[str, Any]],
    observed_option_index_slope: Decimal | None = None,
    atr_buffer_multiplier: Decimal = Decimal("0.20"),
    target_r_multiple: Decimal = Decimal("1.20"),
    max_target_pct: Decimal = Decimal("0.06"),
) -> RealisticRiskPlan | None:
    """Build intraday-realistic long-option SL/target from index structure and option volatility.

    The index structure is primary; option premium structure is a safety cross-check.
    If the resulting structure risk exceeds the configured rupee cap, reject the plan
    rather than widening the stop.
    """
    if entry_premium <= 0 or option_ltp <= 0 or quantity <= 0:
        return None
    slope = observed_option_index_slope or estimate_option_index_slope(index_candles, option_candles)
    index_stop = structure_stop_from_index_candles(
        option_type=option_type,
        index_candles=index_candles,
        atr_buffer_multiplier=atr_buffer_multiplier,
    )
    candidates: list[tuple[str, Decimal]] = []
    if index_ltp is not None and index_stop is not None and slope is not None and slope > 0:
        index_distance = abs(index_ltp - index_stop)
        mapped_stop = floor_to_tick(option_ltp - (index_distance * slope), tick_size)
        candidates.append(("index_structure_mapped_to_option_premium", mapped_stop))
    option_stop = option_structure_stop_from_candles(
        option_candles=option_candles,
        atr_buffer_multiplier=atr_buffer_multiplier,
        tick_size=tick_size,
    )
    if option_stop is not None:
        candidates.append(("option_premium_structure", option_stop))
    if not candidates:
        return None
    usable = [(basis, stop) for basis, stop in candidates if Decimal("0") < stop < entry_premium]
    if not usable:
        return None
    basis, stop_premium = max(usable, key=lambda item: item[1])
    max_loss_points = max_trade_loss / Decimal(quantity) if max_trade_loss > 0 else None
    risk_points = entry_premium - stop_premium
    if max_loss_points is not None and risk_points > max_loss_points:
        return None
    target_points = risk_points * target_r_multiple
    max_target_points = entry_premium * max_target_pct
    if max_target_points > 0:
        target_points = min(target_points, max_target_points)
    if target_points <= 0:
        return None
    target_premium = round_to_tick(entry_premium + target_points, tick_size)
    risk_points = (entry_premium - stop_premium).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    risk_rupees = (risk_points * Decimal(quantity)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    target_points = (target_premium - entry_premium).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return RealisticRiskPlan(
        stop_premium=stop_premium,
        target_premium=target_premium,
        index_stop=index_stop,
        risk_points=risk_points,
        risk_rupees=risk_rupees,
        target_points=target_points,
        raw={
            "basis": basis,
            "index_stop": None if index_stop is None else str(index_stop),
            "observed_option_index_slope": None if slope is None else str(slope),
            "option_structure_stop": None if option_stop is None else str(option_stop),
            "candidate_stops": [{"basis": b, "stop": str(s)} for b, s in candidates],
            "atr_buffer_multiplier": str(atr_buffer_multiplier),
            "target_r_multiple": str(target_r_multiple),
            "max_target_pct": str(max_target_pct),
        },
    )


def compute_profit_lock_stop(
    entry_premium: Decimal,
    highest_premium: Decimal,
    quantity: int,
    *,
    profit_lock_trigger: Decimal,
    profit_lock_step: Decimal,
    tick_size: Decimal,
) -> Decimal | None:
    """Return the legacy stepped profit-lock stop based on best observed open-trade P&L."""
    if profit_lock_trigger <= 0 or profit_lock_step <= 0 or quantity <= 0:
        return None
    high_pnl = (highest_premium - entry_premium) * Decimal(quantity)
    if high_pnl < profit_lock_trigger:
        return None
    locked_steps = ((high_pnl - profit_lock_trigger) / profit_lock_step).to_integral_value(rounding=ROUND_DOWN)
    locked_pnl = (locked_steps * profit_lock_step) + profit_lock_step
    if locked_pnl <= 0:
        return None
    return floor_to_tick(entry_premium + (locked_pnl / Decimal(quantity)), tick_size)


def compute_mfe_ratchet_stop(
    entry_premium: Decimal,
    highest_premium: Decimal,
    quantity: int,
    *,
    risk_rupees: Decimal,
    breakeven_at_r: Decimal,
    ratchet_start_r: Decimal,
    ratchet_giveback_pct: Decimal,
    ratchet_giveback_min_inr: Decimal,
    tick_size: Decimal,
) -> Decimal | None:
    """R-based breakeven + MFE-ratchet trailing stop for long options."""
    if entry_premium <= 0 or quantity <= 0 or risk_rupees <= 0:
        return None
    mfe = (highest_premium - entry_premium) * Decimal(quantity)
    if mfe < risk_rupees * breakeven_at_r:
        return None
    locked_pnl = tick_size * Decimal(quantity)  # breakeven + one tick as cost proxy
    if mfe >= risk_rupees * ratchet_start_r:
        giveback = max(ratchet_giveback_min_inr, mfe * ratchet_giveback_pct / Decimal("100"))
        locked_pnl = max(locked_pnl, mfe - giveback)
    return floor_to_tick(entry_premium + (locked_pnl / Decimal(quantity)), tick_size)


def evaluate_stagnation_exit(
    *,
    pnl: Decimal,
    risk_rupees: Decimal,
    now: datetime,
    entry_time: datetime,
    stagnation_minutes: int,
    stagnation_min_r: Decimal,
    momentum_gone: bool,
) -> str | None:
    if risk_rupees <= 0 or stagnation_minutes <= 0 or not momentum_gone:
        return None
    if (now - entry_time).total_seconds() < stagnation_minutes * 60:
        return None
    if pnl < risk_rupees * stagnation_min_r:
        return "stagnation_exit"
    return None


def evaluate_option_exit(
    ltp: Decimal,
    entry_premium: Decimal,
    stop_premium: Decimal,
    target_premium: Decimal,
    quantity: int,
    *,
    now: datetime,
    entry_time: datetime,
    force_exit_utc: datetime | None,
    highest_premium: Decimal | None = None,
    profit_lock_trigger: Decimal = Decimal("0"),
    profit_lock_step: Decimal = Decimal("0"),
    tick_size: Decimal = Decimal("0.05"),
    target_exit_enabled: bool = True,
) -> tuple[str | None, Decimal | None, Decimal | None]:
    if highest_premium is not None:
        profit_lock_stop = compute_profit_lock_stop(
            entry_premium,
            highest_premium,
            quantity,
            profit_lock_trigger=profit_lock_trigger,
            profit_lock_step=profit_lock_step,
            tick_size=tick_size,
        )
        if profit_lock_stop is not None and profit_lock_stop > stop_premium:
            stop_premium = profit_lock_stop
    if ltp <= stop_premium:
        # A gap through the stop fills at the observed LTP, not the stop level —
        # paper P&L must not pretend the stop price was achievable.
        exit_premium = min(ltp, stop_premium)
        pnl = (exit_premium - entry_premium) * quantity
        reason = "profit_lock_stop" if stop_premium > entry_premium else "stop_loss"
        return reason, exit_premium, pnl.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if target_exit_enabled and ltp >= target_premium:
        pnl = (target_premium - entry_premium) * quantity
        return "target", target_premium, pnl.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if force_exit_utc is not None and now >= force_exit_utc:
        pnl = (ltp - entry_premium) * quantity
        return "force_intraday_exit", ltp, pnl.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return None, None, None


def evaluate_stale_quote_force_exit(
    *,
    entry_premium: Decimal,
    quantity: int,
    now: datetime,
    force_exit_utc: datetime | None,
) -> tuple[str | None, Decimal | None, Decimal | None]:
    """Close a stale-quote paper trade at breakeven once force-exit time passes.

    A stale quote must not let an intraday paper position survive beyond the
    configured close boundary. We intentionally use entry premium instead of a
    stale LTP so realized paper P&L is conservative/auditable.
    """
    if force_exit_utc is None or now < force_exit_utc:
        return None, None, None
    pnl = Decimal("0.00") if quantity else Decimal("0.00")
    return "force_intraday_exit_stale_quote", entry_premium, pnl


def connect_db() -> psycopg.Connection:
    load_dotenv(PROJECT_ROOT / ".env")
    # Pin the session timezone so ts::date / current_date resolve in IST on any host.
    return psycopg.connect(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL), options="-c timezone=Asia/Kolkata")


def apply_migrations() -> None:
    for migration in sorted((PROJECT_ROOT / "migrations").glob("*.sql")):
        subprocess.run(
            [str(PROJECT_ROOT / "scripts" / "psql.sh"), "-h", "127.0.0.1", "-p", "55432", "-d", "finance_tracker", "-f", str(migration)],
            cwd=str(PROJECT_ROOT),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )


def refresh_quotes(symbols: list[str]) -> None:
    if not symbols:
        return
    # Read-only quote ingestion. The called script writes market.quotes only.
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "ingest_fyers_quotes.py"), "--symbols", *symbols],
        cwd=str(PROJECT_ROOT),
        check=True,
        env={**os.environ, "FYERS_LOG_PATH": "/tmp/"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def refresh_today_history(symbols: list[str], *, resolution: str) -> None:
    if not symbols:
        return
    today = now_ist().date().isoformat()
    # Read-only FYERS history ingestion. The called script writes market.candles only.
    subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "ingest_fyers_history.py"),
            "--symbols",
            *symbols,
            "--resolution",
            resolution,
            "--from",
            today,
            "--to",
            today,
        ],
        cwd=str(PROJECT_ROOT),
        check=True,
        env={**os.environ, "FYERS_LOG_PATH": "/tmp/"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def safe_refresh_quotes(symbols: list[str]) -> str | None:
    """Best-effort quote refresh for monitor paths; returns a warning on failure.

    Exit evaluation must continue from the last stored quote when FYERS/token/network
    refresh fails, especially near the intraday force-exit boundary.
    """
    try:
        refresh_quotes(symbols)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()
        tail = detail[-1] if detail else str(exc)
        return f"Quote refresh failed; continuing with last stored DB quotes: {tail}"
    return None


def safe_refresh_today_history(symbols: list[str], *, resolution: str) -> str | None:
    """Best-effort intraday candle refresh for structure/risk checks.

    Entry evaluation can continue from stored candles if FYERS history is temporarily
    unavailable, but the monitor should refresh the candle data it depends on for
    swing breakout and structure-based stop logic.
    """
    try:
        refresh_today_history(symbols, resolution=resolution)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()
        tail = detail[-1] if detail else str(exc)
        return f"Candle refresh failed; continuing with last stored DB candles: {tail}"
    return None


def get_or_init_campaign(cur: psycopg.Cursor, config: CampaignConfig) -> Campaign:
    today_ist = datetime.now(IST).date()
    cur.execute(
        """
        insert into research.option_paper_campaigns(
            name, underlying, underlying_symbol, start_date, starting_capital, active,
            max_premium_exposure, max_daily_loss, max_open_positions, max_trades_per_day,
            stop_loss_pct, target_pct, no_new_trades_after, force_exit_time,
            poll_interval_seconds, notes
        ) values (%s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict(name) do update set
            active = true,
            max_premium_exposure = excluded.max_premium_exposure,
            max_daily_loss = excluded.max_daily_loss,
            max_open_positions = excluded.max_open_positions,
            max_trades_per_day = excluded.max_trades_per_day,
            stop_loss_pct = excluded.stop_loss_pct,
            target_pct = excluded.target_pct,
            no_new_trades_after = excluded.no_new_trades_after,
            force_exit_time = excluded.force_exit_time,
            poll_interval_seconds = excluded.poll_interval_seconds,
            updated_at = now()
        returning campaign_id, name, start_date, starting_capital, max_daily_loss, max_open_positions, max_trades_per_day
        """,
        (
            config.campaign_name,
            config.underlying,
            config.underlying_symbol,
            today_ist,
            config.starting_capital,
            config.max_premium_exposure,
            config.max_daily_loss,
            config.max_open_positions,
            config.max_trades_per_day,
            config.stop_loss_pct,
            config.target_pct,
            config.no_new_trades_after.replace(tzinfo=None),
            config.force_exit_time.replace(tzinfo=None),
            config.poll_interval_seconds,
            config.notes,
        ),
    )
    row = cur.fetchone()
    return Campaign(int(row[0]), str(row[1]), row[2], Decimal(str(row[3])), Decimal(str(row[4])), int(row[5]), int(row[6]))


def upsert_contracts(cur: psycopg.Cursor, contracts: list[FyersOptionContract]) -> int:
    rows = 0
    for contract in contracts:
        cur.execute(
            """
            insert into research.option_contracts(symbol, underlying, expiry, strike, option_type, lot_size, tick_size, raw, updated_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
            on conflict(symbol) do update set
                underlying=excluded.underlying,
                expiry=excluded.expiry,
                strike=excluded.strike,
                option_type=excluded.option_type,
                lot_size=excluded.lot_size,
                tick_size=excluded.tick_size,
                raw=excluded.raw,
                updated_at=now()
            """,
            (
                contract.symbol,
                contract.underlying,
                contract.expiry,
                contract.strike,
                contract.option_type,
                contract.lot_size,
                contract.tick_size,
                json_dumps_safe(contract.raw),
            ),
        )
        rows += 1
    return rows


def refresh_contract_master() -> list[str]:
    contracts = fetch_fyers_banknifty_options()
    today = datetime.now(IST).date()
    active = [c for c in contracts if c.expiry >= today]
    with connect_db() as conn:
        with conn.cursor() as cur:
            rows = upsert_contracts(cur, active)
    expiries = sorted({c.expiry for c in active})[:3]
    return [
        "## BankNifty Options Contract Refresh",
        "Safety: public FYERS master + DB upsert only — no live orders.",
        f"Active/non-expired contracts stored: {rows}",
        f"Nearest expiries: {', '.join(e.isoformat() for e in expiries) if expiries else 'n/a'}",
    ]


def get_quote(cur: psycopg.Cursor, symbol: str, stale_seconds: int) -> tuple[Decimal | None, dict[str, Any], bool]:
    cur.execute(
        """
        select ltp, open, high, low, close, volume, quote_time, updated_at, raw
        from market.quotes
        where symbol=%s
        """,
        (symbol,),
    )
    row = cur.fetchone()
    if not row or row[0] is None:
        return None, {}, True
    now = datetime.now(timezone.utc)
    updated_at = row[7]
    stale = updated_at is None or (now - updated_at).total_seconds() > stale_seconds
    raw = row[8] if isinstance(row[8], dict) else {}
    return Decimal(str(row[0])), {"open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5], "quote_time": row[6], "updated_at": updated_at, "raw": raw}, stale


def get_chain_metrics(cur: psycopg.Cursor, symbol: str, stale_seconds: int) -> tuple[dict[str, Any], bool]:
    """Latest option-chain greeks/IV/OI/bid-ask for one option symbol.

    Read-only lookup of the most recent `market.option_chain_snapshots` row. Returns
    a dict of only the present (non-null) fields plus a staleness flag. The keys are
    chosen so parse_option_quote_metrics picks them up when merged into option_meta,
    enabling the greeks/IV/OI risk guards. Empty dict when no chain row exists."""
    cur.execute(
        """
        select bid, ask, volume, oi, delta, gamma, theta, vega, iv, snapshot_time
        from market.option_chain_snapshots
        where symbol=%s
        order by snapshot_time desc
        limit 1
        """,
        (symbol,),
    )
    row = cur.fetchone()
    if not row:
        return {}, True
    snapshot_time = row[9]
    now = datetime.now(timezone.utc)
    stale = snapshot_time is None or (now - snapshot_time).total_seconds() > stale_seconds
    fields = {
        "bid": row[0], "ask": row[1], "volume": row[2], "oi": row[3],
        "delta": row[4], "gamma": row[5], "theta": row[6], "vega": row[7], "iv": row[8],
    }
    return {k: v for k, v in fields.items() if v is not None}, stale


def get_chain_summary(cur: psycopg.Cursor, underlying: str, stale_seconds: int) -> tuple[dict[str, Any], bool]:
    """Latest option-chain summary (PCR / IV regime / OI buildup) for an underlying.

    Reads the two most recent market.option_chain_summary rows so the OI-buildup
    direction can be derived from the change in total CE vs PE open interest. Returns
    a dict (pcr, iv_regime, oi_buildup_label, totals) plus a staleness flag. Empty
    dict when no summary exists."""
    cur.execute(
        """
        select snapshot_time, pcr, iv_regime, total_ce_oi, total_pe_oi, max_pain_strike, atm_iv
        from market.option_chain_summary
        where underlying=%s
        order by snapshot_time desc
        limit 2
        """,
        (underlying,),
    )
    rows = cur.fetchall()
    if not rows:
        return {}, True
    latest = rows[0]
    snapshot_time = latest[0]
    now = datetime.now(timezone.utc)
    stale = snapshot_time is None or (now - snapshot_time).total_seconds() > stale_seconds

    oi_label: str | None = None
    if len(rows) > 1 and latest[3] is not None and latest[4] is not None and rows[1][3] is not None and rows[1][4] is not None:
        ce_change = int(latest[3]) - int(rows[1][3])
        pe_change = int(latest[4]) - int(rows[1][4])
        if ce_change == 0 and pe_change == 0:
            oi_label = "flat"
        elif pe_change > ce_change:
            oi_label = "put_buildup"
        else:
            oi_label = "call_buildup"

    summary = {
        "pcr": latest[1],
        "iv_regime": latest[2],
        "total_ce_oi": latest[3],
        "total_pe_oi": latest[4],
        "max_pain_strike": latest[5],
        "atm_iv": latest[6],
        "oi_buildup_label": oi_label,
    }
    return summary, stale


def current_campaign(cur: psycopg.Cursor, config: CampaignConfig) -> Campaign:
    cur.execute(
        """
        select campaign_id, name, start_date, starting_capital, max_daily_loss, max_open_positions, max_trades_per_day
        from research.option_paper_campaigns
        where name=%s
        """,
        (config.campaign_name,),
    )
    row = cur.fetchone()
    if not row:
        return get_or_init_campaign(cur, config)
    return Campaign(int(row[0]), str(row[1]), row[2], Decimal(str(row[3])), Decimal(str(row[4])), int(row[5]), int(row[6]))


def get_contracts_from_db(cur: psycopg.Cursor, config: CampaignConfig) -> list[FyersOptionContract]:
    cur.execute(
        """
        select symbol, underlying, expiry, strike, option_type, lot_size, tick_size, raw
        from research.option_contracts
        where underlying=%s and expiry >= current_date
        """,
        (config.underlying,),
    )
    contracts = []
    for row in cur.fetchall():
        contracts.append(FyersOptionContract(str(row[0]), str(row[1]), row[2], Decimal(str(row[3])), str(row[4]), int(row[5]), Decimal(str(row[6])), row[7] if isinstance(row[7], dict) else {}))
    return contracts


def trade_counts(cur: psycopg.Cursor, campaign: Campaign) -> tuple[int, int, Decimal]:
    today = datetime.now(IST).date()
    cur.execute(
        """
        select
            count(*) filter (where status='open') as open_count,
            count(*) filter (where created_at at time zone 'Asia/Kolkata' >= %s::date) as trades_today,
            coalesce(sum(case when status='closed' and exit_time at time zone 'Asia/Kolkata' >= %s::date then realized_pnl else 0 end), 0) as realized_today
        from research.option_paper_trades
        where campaign_id=%s
        """,
        (today, today, campaign.campaign_id),
    )
    row = cur.fetchone()
    return int(row[0] or 0), int(row[1] or 0), Decimal(str(row[2] or 0))


def insert_event(cur: psycopg.Cursor, trade_id: int, event_type: str, premium: Decimal | None, quantity: int | None, message: str, raw: dict[str, Any] | None = None) -> None:
    cur.execute(
        """
        insert into research.option_paper_trade_events(option_trade_id, event_type, premium, quantity, message, raw)
        values (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (trade_id, event_type, premium, quantity, message, json_dumps_safe(raw or {})),
    )


def _insert_no_entry_decision(
    cur: psycopg.Cursor,
    config: CampaignConfig,
    now_local: datetime,
    *,
    mode: str,
    blocker: str,
    reason: str,
    metrics: dict[str, Any] | None,
    raw: dict[str, Any] | None,
    campaign: Campaign | None,
) -> None:
    """Append one no-entry audit row on ``cur`` (append-only, never deduped)."""
    camp = campaign or current_campaign(cur, config)
    cur.execute(
        """
        insert into research.option_paper_no_entry_decisions
            (campaign_id, trade_date, decision_time, decision_minute, mode, blocker, reason, metrics, raw)
        values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        """,
        (
            camp.campaign_id,
            now_local.date(),
            now_local,
            now_local.replace(second=0, microsecond=0),
            mode,
            blocker,
            reason,
            json_dumps_safe(metrics or {}),
            json_dumps_safe(raw or {}),
        ),
    )


def record_no_entry_decision(
    config: CampaignConfig,
    *,
    mode: str,
    blocker: str,
    reason: str,
    metrics: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
    campaign: Campaign | None = None,
    cur: psycopg.Cursor | None = None,
) -> None:
    """Best-effort persist of one no-entry tick decision for later audit.

    Paper/research only — appends one row to
    research.option_paper_no_entry_decisions. This is intentionally silent and
    failure-tolerant: it must never break the scan/tick path or surface noise on
    a normal no-trade tick. The table is append-only: every call inserts a new
    row so each no-entry tick stays independently auditable. Two ticks in the
    same minute with the same blocker are two rows, never collapsed into one, so
    the daily report counts every tick.

    When ``cur`` is supplied the insert runs inside the caller's open
    transaction. This matters on the first-run scan/tick path: if
    ``current_campaign`` auto-created the campaign on that same cursor, the row
    is not yet committed, so a separate connection's FK insert could not see it
    and the first no-entry decision would be silently dropped. Writing on the
    caller's cursor keeps the campaign and its audit row in one transaction. A
    savepoint guards the caller's transaction so a failed audit insert can never
    poison it. Callers without a cursor (tick non-boundary, pre-DB gates) keep
    the best-effort separate-connection behavior.
    """
    try:
        now_local = now_ist()
        fields = dict(mode=mode, blocker=blocker, reason=reason, metrics=metrics, raw=raw, campaign=campaign)
        if cur is not None:
            conn = getattr(cur, "connection", None)
            if conn is not None and hasattr(conn, "transaction"):
                # Savepoint: a failed audit insert rolls back to here, leaving
                # any auto-created campaign on the caller's transaction intact.
                with conn.transaction():
                    _insert_no_entry_decision(cur, config, now_local, **fields)
            else:
                _insert_no_entry_decision(cur, config, now_local, **fields)
            return
        with connect_db() as conn:
            with conn.cursor() as own_cur:
                _insert_no_entry_decision(own_cur, config, now_local, **fields)
    except Exception:
        # Audit logging is best-effort; never let it break the paper scan/tick path.
        return


def now_ist() -> datetime:
    return datetime.now(IST)


def combine_ist_time(day: date, value: dtime) -> datetime:
    return datetime.combine(day, value.replace(tzinfo=None), tzinfo=IST)


def should_run_entry_scan(now: datetime, interval_minutes: int) -> bool:
    """Return true only on the configured minute boundary for pre-entry scans."""
    if interval_minutes <= 1:
        return True
    local_now = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    return local_now.minute % interval_minutes == 0


def has_open_option_trade(config: CampaignConfig) -> bool:
    """Lightweight guard used by the wrapper before starting fast 15s monitoring."""
    with connect_db() as conn:
        with conn.cursor() as cur:
            campaign = current_campaign(cur, config)
            cur.execute(
                """
                select exists(
                    select 1
                    from research.option_paper_trades
                    where campaign_id=%s and status='open'
                )
                """,
                (campaign.campaign_id,),
            )
            return bool(cur.fetchone()[0])


ENGINE_CONTROL_NAME = "banknifty_options_paper"


def control_state_paused(cur: psycopg.Cursor, engine: str = ENGINE_CONTROL_NAME) -> bool:
    """True when the dashboard control plane paused this engine.

    Pausing stops new paper entries only; open positions keep being managed
    (stop ratchet, stagnation, force-exit at session close). Missing control
    tables (migration 015 not applied) never block the engine.
    """
    cur.execute("select to_regclass('research.control_state')")
    if cur.fetchone()[0] is None:
        return False
    cur.execute("select paused from research.control_state where engine=%s", (engine,))
    row = cur.fetchone()
    return bool(row and row[0])


def partition_force_exit_claims(
    rows: list[tuple[int, Any]],
    open_trade_ids: set[int],
) -> tuple[dict[int, int], list[tuple[int, str]]]:
    """Split pending force-exit request rows into claims and rejections.

    rows are (request_id, payload) oldest first. Returns ({trade_id: request_id},
    [(request_id, reject_message), ...]). Requests for trades that are not open
    in this campaign are rejected, and duplicates for the same trade are
    rejected so one operator click can never close twice.
    """
    claims: dict[int, int] = {}
    rejects: list[tuple[int, str]] = []
    for request_id, payload in rows:
        payload = payload if isinstance(payload, dict) else {}
        raw_trade_id = payload.get("trade_id")
        try:
            trade_id = int(raw_trade_id)
        except (TypeError, ValueError):
            rejects.append((int(request_id), f"invalid trade_id {raw_trade_id!r}"))
            continue
        if trade_id not in open_trade_ids:
            rejects.append((int(request_id), f"trade {trade_id} is not an open paper trade of this campaign"))
            continue
        if trade_id in claims:
            rejects.append((int(request_id), f"duplicate force-exit request for trade {trade_id}"))
            continue
        claims[trade_id] = int(request_id)
    return claims, rejects


def evaluate_manual_force_exit(
    *,
    entry_premium: Decimal,
    ltp: Decimal | None,
    quantity: int,
) -> tuple[Decimal, Decimal]:
    """Exit premium and paper P&L for an operator-requested flatten.

    The operator asked to flatten, so we close even on a stale quote (noted in
    the audit message); with no stored quote at all we close at entry premium
    (breakeven) rather than inventing a price.
    """
    exit_premium = ltp if ltp is not None else entry_premium
    pnl = ((exit_premium - entry_premium) * Decimal(quantity)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return exit_premium, pnl


def claim_force_exit_requests(cur: psycopg.Cursor, open_trade_ids: set[int]) -> dict[int, int]:
    """Claim today's pending force-exit requests for this engine's open trades.

    Runs inside the monitor transaction (FOR UPDATE SKIP LOCKED) so a request is
    honored exactly once. Requests from previous IST days are never claimed; the
    control applier expires those.
    """
    cur.execute("select to_regclass('research.control_requests')")
    if cur.fetchone()[0] is None:
        return {}
    cur.execute(
        """
        select request_id, payload
        from research.control_requests
        where engine = %s and action_type = 'force_exit' and status = 'pending'
          and (requested_at at time zone 'Asia/Kolkata')::date = (now() at time zone 'Asia/Kolkata')::date
        order by requested_at, request_id
        for update skip locked
        """,
        (ENGINE_CONTROL_NAME,),
    )
    claims, rejects = partition_force_exit_claims(cur.fetchall(), open_trade_ids)
    for request_id, message in rejects:
        cur.execute(
            "update research.control_requests set status='rejected', processed_at=now(), result_message=%s where request_id=%s",
            (message, request_id),
        )
    return claims


def pct_from_open(ltp: Decimal, quote_meta: dict[str, Any]) -> tuple[Decimal | None, Decimal | None, str | None]:
    open_value = quote_meta.get("open")
    if open_value is None:
        return None, None, "open price missing"
    open_dec = Decimal(str(open_value))
    if open_dec <= 0:
        return None, open_dec, "open price invalid"
    pct = ((ltp - open_dec) / open_dec * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return pct, open_dec, None


def quote_raw_value(meta: dict[str, Any], *keys: str) -> Any:
    raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}
    v = raw.get("v") if isinstance(raw.get("v"), dict) else {}
    for key in keys:
        if key in meta and meta[key] is not None:
            return meta[key]
        if key in v and v[key] is not None:
            return v[key]
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(Decimal(str(value)))
    except Exception:
        return None


def get_recent_candles(cur: psycopg.Cursor, symbol: str, *, limit: int = 8, resolution: str = "1") -> list[dict[str, Any]]:
    cur.execute(
        """
        select ts, open, high, low, close, volume
        from market.candles
        where symbol=%s and resolution=%s and ts::date = current_date
        order by ts desc
        limit %s
        """,
        (symbol, resolution, limit),
    )
    rows = list(reversed(cur.fetchall()))
    return [
        {"ts": row[0], "open": Decimal(str(row[1])), "high": Decimal(str(row[2])), "low": Decimal(str(row[3])), "close": Decimal(str(row[4])), "volume": row[5]}
        for row in rows
    ]


def get_day_range_and_adr10(cur: psycopg.Cursor, symbol: str, *, resolution: str = "5") -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    cur.execute(
        """
        select max(high), min(low)
        from market.candles
        where symbol=%s and resolution=%s and ts::date = current_date
        """,
        (symbol, resolution),
    )
    row = cur.fetchone()
    day_high = decimal_or_none(row[0] if row else None)
    day_low = decimal_or_none(row[1] if row else None)
    cur.execute(
        """
        select avg(day_range)::numeric
        from (
            select ts::date as day, max(high) - min(low) as day_range
            from market.candles
            where symbol=%s and resolution=%s and ts::date < current_date
            group by ts::date
            having max(high) > min(low)
            order by ts::date desc
            limit 10
        ) ranges
        """,
        (symbol, resolution),
    )
    adr_row = cur.fetchone()
    return day_high, day_low, decimal_or_none(adr_row[0] if adr_row else None)


def get_missing_constituent_candle_coverage(
    cur: psycopg.Cursor,
    constituents: tuple[BankNiftyConstituent, ...],
    *,
    resolution: str = "5",
) -> list[BankNiftyConstituent]:
    """Return constituents with no intraday candle rows in ``market.candles`` today.

    Read-only coverage check used by the candle-coverage report so a missing
    constituent candle feed is surfaced explicitly rather than silently skewing
    breadth/structure logic. A constituent counts as covered once it has at least
    one row for the given resolution on the current IST trading date.
    """
    if not constituents:
        return []
    symbols = [c.fyers_symbol for c in constituents]
    cur.execute(
        """
        select distinct symbol
        from market.candles
        where symbol = any(%s) and resolution=%s and ts::date = current_date
        """,
        (symbols, resolution),
    )
    covered = {row[0] for row in cur.fetchall()}
    return [c for c in constituents if c.fyers_symbol not in covered]


def confluence_levels_from_candles(candles: list[dict[str, Any]], *, direction: str, structure_lookback: int = 8) -> list[Decimal]:
    if len(candles) < 3:
        return []
    bullish = direction == "CE"
    levels: list[Decimal] = []
    orb = candles[:3]
    levels.append(max(candle_decimal(c, "high") for c in orb) if bullish else min(candle_decimal(c, "low") for c in orb))
    prior = candles[:-1]
    if prior:
        recent = prior[-structure_lookback:]
        levels.append(max(candle_decimal(c, "high") for c in recent) if bullish else min(candle_decimal(c, "low") for c in recent))
    deduped: list[Decimal] = []
    for level in levels:
        if level not in deduped:
            deduped.append(level)
    return deduped


def option_recent_volume_ok(candles: list[dict[str, Any]], *, lookback: int = 3) -> bool:
    recent = candles[-lookback:]
    if not recent:
        return False
    return any(int(c.get("volume") or 0) > 0 for c in recent)


def quote_meta_as_candle(current_ltp: Decimal, quote_meta: dict[str, Any]) -> dict[str, Any] | None:
    open_dec = decimal_or_none(quote_meta.get("open"))
    high_dec = decimal_or_none(quote_meta.get("high"))
    low_dec = decimal_or_none(quote_meta.get("low"))
    if open_dec is None or high_dec is None or low_dec is None:
        return None
    return {"open": open_dec, "high": high_dec, "low": low_dec, "close": current_ltp, "volume": quote_meta.get("volume")}


def get_relative_volume(cur: psycopg.Cursor, symbol: str, current_volume: int | None, *, lookback_days: int = 20) -> Decimal | None:
    if current_volume is None or current_volume <= 0:
        return None
    cur.execute(
        """
        select avg(day_volume)::numeric
        from (
            select ts::date as day, max(volume) as day_volume
            from market.candles
            where symbol=%s and volume is not null and ts::date < current_date
            group by ts::date
            order by ts::date desc
            limit %s
        ) daily
        """,
        (symbol, lookback_days),
    )
    row = cur.fetchone()
    avg_volume = decimal_or_none(row[0] if row else None)
    if avg_volume is None or avg_volume <= 0:
        return None
    return (Decimal(current_volume) / avg_volume).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def major_constituent_jump_reason(
    move: ConstituentMove,
    *,
    direction: str,
    major_jump_threshold_pct: Decimal,
    rel_volume_threshold: Decimal,
) -> ConstituentJumpReason | None:
    bullish = direction == "CE"
    if bullish and move.pct_from_open < major_jump_threshold_pct:
        return None
    if not bullish and move.pct_from_open > -major_jump_threshold_pct:
        return None
    vwap_confirmed = move.vwap is not None and ((move.ltp >= move.vwap) if bullish else (move.ltp <= move.vwap))
    relvol_confirmed = move.relative_volume is not None and move.relative_volume >= rel_volume_threshold
    side = "upside" if bullish else "downside"
    relvol_text = f"rel-vol {move.relative_volume}x" if move.relative_volume is not None else "rel-vol n/a"
    vwap_text = f"VWAP {move.vwap}" if move.vwap is not None else "VWAP n/a"
    summary = (
        f"{move.symbol} major {side} jump {move.pct_from_open:+.2f}% "
        f"({vwap_text}, {relvol_text}); trigger news/reason review."
    )
    return ConstituentJumpReason(move.symbol, direction, move.pct_from_open, move.contribution, vwap_confirmed, relvol_confirmed, summary)


def top_directional_moves(moves: list[ConstituentMove], *, direction: str, limit: int = 3) -> list[ConstituentMove]:
    if direction == "CE":
        directional = [move for move in moves if move.pct_from_open > 0]
        return sorted(directional, key=lambda move: move.contribution, reverse=True)[:limit]
    directional = [move for move in moves if move.pct_from_open < 0]
    return sorted(directional, key=lambda move: move.contribution)[:limit]


def evaluate_vwap_volume_confirmation(
    moves: list[ConstituentMove],
    *,
    direction: str,
    min_confirming_top_movers: int,
    rel_volume_threshold: Decimal,
) -> ConfirmationDecision:
    if min_confirming_top_movers <= 0:
        return ConfirmationDecision(True, ["VWAP/relative-volume confirmation disabled by threshold."], [], {})
    top_moves = top_directional_moves(moves, direction=direction, limit=max(3, min_confirming_top_movers))
    confirmed: list[str] = []
    raw_moves: list[dict[str, Any]] = []
    bullish = direction == "CE"
    for move in top_moves:
        vwap_ok = move.vwap is not None and ((move.ltp >= move.vwap) if bullish else (move.ltp <= move.vwap))
        relvol_ok = move.relative_volume is not None and move.relative_volume >= rel_volume_threshold
        raw_moves.append({
            "symbol": move.symbol,
            "ltp": str(move.ltp),
            "vwap": None if move.vwap is None else str(move.vwap),
            "relative_volume": None if move.relative_volume is None else str(move.relative_volume),
            "vwap_confirmed": vwap_ok,
            "relative_volume_confirmed": relvol_ok,
        })
        if vwap_ok and relvol_ok:
            confirmed.append(move.symbol)
    if len(confirmed) >= min_confirming_top_movers:
        return ConfirmationDecision(
            True,
            [f"VWAP/relative-volume confirmation passed: {', '.join(confirmed)}."],
            confirmed,
            {"top_moves": raw_moves},
        )
    return ConfirmationDecision(
        False,
        [
            f"VWAP/relative-volume confirmation failed: {len(confirmed)}/{min_confirming_top_movers} top directional movers confirmed."
        ],
        confirmed,
        {"top_moves": raw_moves},
    )


def evaluate_weighted_vwap_side(
    moves: list[ConstituentMove],
    *,
    direction: str,
    min_side_pct: Decimal,
) -> ConfirmationDecision:
    if not moves:
        return ConfirmationDecision(False, ["No trade: no constituent VWAP-side data available."], [], {})
    total_weight = sum((m.normalized_weight for m in moves), Decimal("0"))
    if total_weight <= 0:
        return ConfirmationDecision(False, ["No trade: constituent weights unavailable for VWAP-side confirmation."], [], {})
    bullish = direction == "CE"
    side_weight = sum(
        (
            m.normalized_weight
            for m in moves
            if m.vwap is not None and ((m.ltp >= m.vwap) if bullish else (m.ltp <= m.vwap))
        ),
        Decimal("0"),
    )
    side_pct = (side_weight / total_weight * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    side_text = "above" if bullish else "below"
    allowed = side_pct >= min_side_pct
    reason = (
        f"Weighted constituent VWAP-side {'passed' if allowed else 'failed'}: "
        f"{side_pct}% weight is {side_text} own VWAP vs required {min_side_pct}%."
    )
    return ConfirmationDecision(allowed, [reason], [], {"weighted_vwap_side_pct": str(side_pct), "required_pct": str(min_side_pct)})


def evaluate_lunch_chop_guard(
    now: datetime,
    *,
    day_high: Decimal | None,
    day_low: Decimal | None,
    adr10: Decimal | None,
    index_rel_volume: Decimal | None,
    window_start: dtime,
    window_end: dtime,
    min_range_vs_adr: Decimal,
    min_relvol: Decimal,
) -> ConfirmationDecision:
    local_now = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    if not (window_start <= local_now.time().replace(tzinfo=None) <= window_end):
        return ConfirmationDecision(True, ["Lunch-chop guard inactive outside configured window."], [], {})
    if day_high is None or day_low is None or adr10 is None or adr10 <= 0 or index_rel_volume is None:
        return ConfirmationDecision(False, ["No trade: lunch-chop guard lacks day range/ADR/rel-volume data."], [], {})
    day_range = day_high - day_low
    required_range = adr10 * min_range_vs_adr
    allowed = day_range >= required_range and index_rel_volume >= min_relvol
    reason = (
        f"Lunch-chop guard {'passed' if allowed else 'blocked'}: range {day_range} vs required {required_range}; "
        f"index rel-vol {index_rel_volume}x vs required {min_relvol}x."
    )
    return ConfirmationDecision(allowed, [reason], [], {"day_range": str(day_range), "required_range": str(required_range), "index_rel_volume": str(index_rel_volume)})


def evaluate_chop_regime(
    candles: list[dict[str, Any]],
    *,
    lookback_candles: int,
    max_net_move_pct: Decimal,
    max_vwap_crosses: int,
) -> ConfirmationDecision:
    recent = candles[-lookback_candles:]
    if len(recent) < max(3, lookback_candles):
        return ConfirmationDecision(True, ["Chop guard skipped: insufficient candles."], [], {"candles": len(recent)})
    first = candle_decimal(recent[0], "close")
    last = candle_decimal(recent[-1], "close")
    if first <= 0:
        return ConfirmationDecision(True, ["Chop guard skipped: invalid first close."], [], {})
    net_pct = ((last - first).copy_abs() / first * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    proxy = sum((candle_decimal(c, "high") + candle_decimal(c, "low") + candle_decimal(c, "close")) / Decimal("3") for c in recent) / Decimal(len(recent))
    sides = [1 if candle_decimal(c, "close") >= proxy else -1 for c in recent]
    crosses = sum(1 for prev, cur in zip(sides, sides[1:]) if prev != cur)
    allowed = not (net_pct < max_net_move_pct and crosses >= max_vwap_crosses)
    reason = f"Chop guard {'passed' if allowed else 'blocked'}: net move {net_pct}% over {len(recent)} candles; VWAP-proxy crosses {crosses}."
    return ConfirmationDecision(allowed, [reason], [], {"net_move_pct": str(net_pct), "vwap_proxy_crosses": crosses, "vwap_proxy": str(proxy)})


def evaluate_pullback_continuation(
    *,
    direction: str,
    candles: list[dict[str, Any]],
    confluence_levels: list[Decimal],
    breakout_buffer_pct: Decimal,
    level_hold_buffer_pct: Decimal,
    structure_stop_buffer_pct: Decimal,
    leg_lookback_candles: int,
    max_pullback_candles: int,
) -> IndexStructureSignal:
    usable = candles[-max(leg_lookback_candles + max_pullback_candles + 2, 3):]
    if len(usable) < 4 or not confluence_levels:
        return IndexStructureSignal(False, "No trade: insufficient candles/confluence for pullback continuation.", None, None, {"candles": len(usable)})
    trigger = usable[-1]
    prior_trigger = usable[-2]
    bullish = direction == "CE"
    trigger_ok = candle_decimal(trigger, "close") > candle_decimal(prior_trigger, "high") if bullish else candle_decimal(trigger, "close") < candle_decimal(prior_trigger, "low")
    if not trigger_ok:
        return IndexStructureSignal(False, "No trade: pullback trigger candle has not resumed through prior candle extreme.", None, None, {})

    earliest_leg = max(1, len(usable) - 1 - max_pullback_candles)
    latest_leg = len(usable) - 2
    for leg_idx in range(latest_leg - 1, earliest_leg - 1, -1):
        leg = usable[leg_idx]
        prior = usable[:leg_idx]
        if not prior:
            continue
        session_extreme_ok = candle_decimal(leg, "high") > max(candle_decimal(c, "high") for c in prior) if bullish else candle_decimal(leg, "low") < min(candle_decimal(c, "low") for c in prior)
        if not session_extreme_ok:
            continue
        for level in confluence_levels:
            required = level * (Decimal("1") + breakout_buffer_pct / Decimal("100")) if bullish else level * (Decimal("1") - breakout_buffer_pct / Decimal("100"))
            broke_level = candle_decimal(leg, "high") >= required if bullish else candle_decimal(leg, "low") <= required
            if not broke_level:
                continue
            pullback = usable[leg_idx + 1:-1]
            if not pullback or len(pullback) > max_pullback_candles:
                continue
            hold_level = level * (Decimal("1") - level_hold_buffer_pct / Decimal("100")) if bullish else level * (Decimal("1") + level_hold_buffer_pct / Decimal("100"))
            closes_hold = all((candle_decimal(c, "close") >= level if bullish else candle_decimal(c, "close") <= level) for c in pullback)
            extremes_hold = all((candle_decimal(c, "low") >= hold_level if bullish else candle_decimal(c, "high") <= hold_level) for c in pullback)
            if closes_hold and extremes_hold:
                if bullish:
                    pullback_extreme = min(candle_decimal(c, "low") for c in pullback)
                    stop = (pullback_extreme * (Decimal("1") - structure_stop_buffer_pct / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                else:
                    pullback_extreme = max(candle_decimal(c, "high") for c in pullback)
                    stop = (pullback_extreme * (Decimal("1") + structure_stop_buffer_pct / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                return IndexStructureSignal(True, f"Pullback continuation confirmed: retest held broken level {level} and trigger resumed.", stop, level, {"leg_index": leg_idx, "pullback_candles": len(pullback)})
    return IndexStructureSignal(False, "No trade: no valid pullback/retest continuation after a confluence breakout.", None, None, {})


def candle_decimal(candle: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(candle[key]))


def evaluate_index_structure_signal(
    *,
    direction: str,
    current_ltp: Decimal,
    quote_meta: dict[str, Any],
    candles: list[dict[str, Any]],
    breakout_buffer_pct: Decimal,
    stop_buffer_pct: Decimal,
) -> IndexStructureSignal:
    usable = list(candles)
    if len(usable) < 2:
        fallback = quote_meta_as_candle(current_ltp, quote_meta)
        if fallback is not None:
            usable = [fallback]
    if len(usable) < 2:
        return IndexStructureSignal(False, "No trade: insufficient intraday index swing data for structure confirmation.", None, None, {"candles": len(usable)})
    prior = usable[:-1]
    last = usable[-1]
    if direction == "CE":
        reference = max(candle_decimal(c, "high") for c in prior)
        required = reference * (Decimal("1") + breakout_buffer_pct / Decimal("100"))
        stop_base = candle_decimal(last, "low")
        stop = (stop_base * (Decimal("1") - stop_buffer_pct / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if current_ltp >= required:
            return IndexStructureSignal(True, f"Index structure confirmed: BankNifty broke above prior swing high {reference}.", stop, reference, {"required_breakout": str(required), "candles": len(usable)})
        return IndexStructureSignal(False, f"No trade: BankNifty has not broken prior swing high {reference}.", stop, reference, {"required_breakout": str(required), "candles": len(usable)})
    reference = min(candle_decimal(c, "low") for c in prior)
    required = reference * (Decimal("1") - breakout_buffer_pct / Decimal("100"))
    stop_base = candle_decimal(last, "high")
    stop = (stop_base * (Decimal("1") + stop_buffer_pct / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if current_ltp <= required:
        return IndexStructureSignal(True, f"Index structure confirmed: BankNifty broke below prior swing low {reference}.", stop, reference, {"required_breakdown": str(required), "candles": len(usable)})
    return IndexStructureSignal(False, f"No trade: BankNifty has not broken prior swing low {reference}.", stop, reference, {"required_breakdown": str(required), "candles": len(usable)})


def compute_swing_trailing_stop(
    *,
    direction: str,
    current_stop: Decimal,
    candles: list[dict[str, Any]],
    stop_buffer_pct: Decimal,
) -> Decimal | None:
    if not candles:
        return None
    if direction == "CE":
        latest_low = candle_decimal(candles[-1], "low")
        candidate = (latest_low * (Decimal("1") - stop_buffer_pct / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        return candidate if candidate > current_stop else None
    latest_high = candle_decimal(candles[-1], "high")
    candidate = (latest_high * (Decimal("1") + stop_buffer_pct / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return candidate if candidate < current_stop else None


def evaluate_index_structure_exit(
    *,
    option_type: str,
    index_ltp: Decimal | None,
    structure_stop: Decimal | None,
    option_ltp: Decimal,
    entry_premium: Decimal,
    quantity: int,
) -> tuple[str | None, Decimal | None, Decimal | None]:
    if index_ltp is None or structure_stop is None:
        return None, None, None
    breached = index_ltp <= structure_stop if option_type == "CE" else index_ltp >= structure_stop
    if not breached:
        return None, None, None
    pnl = ((option_ltp - entry_premium) * Decimal(quantity)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return "index_structure_stop", option_ltp, pnl


def normalized_constituent_weights(constituents: tuple[BankNiftyConstituent, ...]) -> dict[str, Decimal]:
    if not constituents:
        return {}
    explicit_weights = [c.weight for c in constituents]
    if all(weight is not None and weight > 0 for weight in explicit_weights):
        total = sum((weight for weight in explicit_weights if weight is not None), Decimal("0"))
        if total > 0:
            return {c.fyers_symbol: (c.weight or Decimal("0")) / total for c in constituents}
    equal = Decimal("1") / Decimal(len(constituents))
    return {c.fyers_symbol: equal for c in constituents}


def get_constituent_moves(
    cur: psycopg.Cursor,
    constituents: tuple[BankNiftyConstituent, ...],
    stale_seconds: int,
) -> tuple[list[ConstituentMove], list[str]]:
    weights = normalized_constituent_weights(constituents)
    symbol_to_name = {c.fyers_symbol: c.symbol for c in constituents}
    moves: list[ConstituentMove] = []
    missing: list[str] = []
    for constituent in constituents:
        ltp, meta, stale = get_quote(cur, constituent.fyers_symbol, stale_seconds)
        if ltp is None or stale:
            missing.append(constituent.symbol)
            continue
        pct, open_dec, error = pct_from_open(ltp, meta)
        if pct is None or open_dec is None:
            missing.append(f"{constituent.symbol}({error})")
            continue
        weight = weights.get(constituent.fyers_symbol, Decimal("0"))
        vwap = decimal_or_none(quote_raw_value(meta, "atp", "vwap", "avg_price"))
        volume = int_or_none(quote_raw_value(meta, "volume", "vol_traded_today"))
        relative_volume = get_relative_volume(cur, constituent.fyers_symbol, volume)
        moves.append(
            ConstituentMove(
                symbol=symbol_to_name.get(constituent.fyers_symbol, constituent.symbol),
                fyers_symbol=constituent.fyers_symbol,
                ltp=ltp,
                open=open_dec,
                pct_from_open=pct,
                normalized_weight=weight,
                contribution=(weight * pct).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                vwap=vwap,
                volume=volume,
                relative_volume=relative_volume,
            )
        )
    return moves, missing


def top_move_summary(moves: list[ConstituentMove], *, direction: str, limit: int = 3) -> str:
    if direction == "CE":
        ordered = sorted(moves, key=lambda move: move.contribution, reverse=True)
        label = "top positives"
    else:
        ordered = sorted(moves, key=lambda move: move.contribution)
        label = "top negatives"
    parts = [f"{m.symbol} {m.pct_from_open:+.2f}%" for m in ordered[:limit]]
    return f"{label}: {', '.join(parts) if parts else 'n/a'}"


def evaluate_constituent_led_direction(
    *,
    underlying_ltp: Decimal,
    underlying_meta: dict[str, Any],
    moves: list[ConstituentMove],
    missing: list[str],
    config: CampaignConfig,
) -> DirectionSignal:
    index_pct, index_open, index_error = pct_from_open(underlying_ltp, underlying_meta)
    if index_pct is None or index_open is None:
        return DirectionSignal(None, f"No trade: BankNifty index {index_error}; skip.", {"missing_constituents": missing})
    if not moves:
        return DirectionSignal(None, "No trade: no fresh constituent quotes available.", {"index_pct": str(index_pct), "missing_constituents": missing})

    available_weight = sum((move.normalized_weight for move in moves), Decimal("0"))
    coverage_pct = (available_weight * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if coverage_pct < config.min_constituent_coverage_pct:
        return DirectionSignal(
            None,
            f"No trade: fresh BankNifty constituent coverage {coverage_pct}% is below {config.min_constituent_coverage_pct}%.",
            {"index_pct": str(index_pct), "coverage_pct": str(coverage_pct), "missing_constituents": missing},
        )

    weighted_pct = (sum((move.contribution for move in moves), Decimal("0")) / available_weight).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pos_weight_pct = (sum((move.normalized_weight for move in moves if move.pct_from_open > 0), Decimal("0")) / available_weight * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    neg_weight_pct = (sum((move.normalized_weight for move in moves if move.pct_from_open < 0), Decimal("0")) / available_weight * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    raw = {
        "index_ltp": str(underlying_ltp),
        "index_open": str(index_open),
        "index_pct": str(index_pct),
        "weighted_constituent_pct": str(weighted_pct),
        "positive_weight_pct": str(pos_weight_pct),
        "negative_weight_pct": str(neg_weight_pct),
        "coverage_pct": str(coverage_pct),
        "missing_constituents": missing,
        "constituents": [
            {
                "symbol": move.symbol,
                "fyers_symbol": move.fyers_symbol,
                "ltp": str(move.ltp),
                "open": str(move.open),
                "pct_from_open": str(move.pct_from_open),
                "normalized_weight": str(move.normalized_weight),
                "contribution": str(move.contribution),
                "vwap": None if move.vwap is None else str(move.vwap),
                "volume": move.volume,
                "relative_volume": None if move.relative_volume is None else str(move.relative_volume),
            }
            for move in moves
        ],
    }

    if (
        weighted_pct >= config.signal_threshold_pct
        and pos_weight_pct >= config.min_directional_weight_pct
        and index_pct >= config.min_index_confirmation_pct
    ):
        reason = (
            f"Bullish constituent-led BankNifty signal: constituents weighted {weighted_pct:+.2f}% vs open, "
            f"positive weight {pos_weight_pct}%, index {index_pct:+.2f}% vs open; {top_move_summary(moves, direction='CE')}."
        )
        return DirectionSignal("CE", reason, raw)

    if (
        weighted_pct <= -config.signal_threshold_pct
        and neg_weight_pct >= config.min_directional_weight_pct
        and index_pct <= -config.min_index_confirmation_pct
    ):
        reason = (
            f"Bearish constituent-led BankNifty signal: constituents weighted {weighted_pct:+.2f}% vs open, "
            f"negative weight {neg_weight_pct}%, index {index_pct:+.2f}% vs open; {top_move_summary(moves, direction='PE')}."
        )
        return DirectionSignal("PE", reason, raw)

    reason = (
        f"No trade: constituent/index confirmation not aligned. Constituents weighted {weighted_pct:+.2f}% "
        f"vs threshold ±{config.signal_threshold_pct}%, positive/negative weight {pos_weight_pct}%/{neg_weight_pct}%, "
        f"index {index_pct:+.2f}% vs required ±{config.min_index_confirmation_pct}%."
    )
    return DirectionSignal(None, reason, raw)


def scan_for_entry(config: CampaignConfig, *, refresh: bool = False, quiet_no_change: bool = False, dry_run: bool = False, run_context: str = "scan") -> list[str]:
    lines = ["## BankNifty Options Paper Scan", "Safety: paper only — no FYERS orders placed.", ""]
    audit_cur: psycopg.Cursor | None = None

    def no_trade(
        blocker: str,
        extra_lines: list[str],
        *,
        reason: str | None = None,
        metrics: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
        campaign: Campaign | None = None,
    ) -> list[str]:
        # Persist the top gate decision for audit even when the cron is quiet, then
        # return the display lines (silent on no-change ticks, verbose otherwise).
        # Gates inside a DB block use that open cursor so the audit row lands in
        # the same transaction as any just-auto-created campaign.
        record_no_entry_decision(
            config,
            mode=run_context,
            blocker=blocker,
            reason=reason if reason is not None else " ".join(str(part) for part in extra_lines),
            metrics=metrics,
            raw=raw,
            campaign=campaign,
            cur=audit_cur,
        )
        return [] if quiet_no_change else lines + extra_lines

    strategy_card = selected_constituent_led_strategy(config)
    if strategy_card is None:
        message = "No trade: no enabled paper-safe entry strategy is runnable in the strategy router."
        return no_trade("no_strategy", [message])
    refresh_warnings: list[str] = []
    if refresh:
        warning = safe_refresh_quotes([config.underlying_symbol, *[c.fyers_symbol for c in config.constituents]])
        if warning:
            refresh_warnings.append(warning)
        if config.index_structure_confirmation_enabled or config.realistic_risk_enabled:
            # Refresh constituent candles alongside the index so per-constituent
            # structure/breadth checks see the same intraday coverage that quotes do.
            warning = safe_refresh_today_history(
                [config.underlying_symbol, *[c.fyers_symbol for c in config.constituents]],
                resolution=config.structure_candle_resolution,
            )
            if warning:
                refresh_warnings.append(warning)
    with connect_db() as conn:
        with conn.cursor() as cur:
            audit_cur = cur
            campaign = current_campaign(cur, config)
            if control_state_paused(cur):
                return no_trade(
                    "engine_paused",
                    ["Engine paused via dashboard control plane; no new paper entries (open positions stay managed)."],
                    campaign=campaign,
                )
            open_count, trades_today, realized_today = trade_counts(cur, campaign)
            now_local = now_ist()
            count_metrics = {"open_count": open_count, "trades_today": trades_today, "realized_today": str(realized_today)}
            if now_local.time() < config.no_new_trades_before.replace(tzinfo=None):
                return no_trade(
                    "before_window",
                    [f"No new trades before {config.no_new_trades_before.strftime('%H:%M')} IST; ORB must form first."],
                    metrics=count_metrics,
                    campaign=campaign,
                )
            if now_local.time() >= config.no_new_trades_after.replace(tzinfo=None):
                return no_trade(
                    "after_window",
                    [f"No new trades after {config.no_new_trades_after.strftime('%H:%M')} IST."],
                    metrics=count_metrics,
                    campaign=campaign,
                )
            if open_count >= campaign.max_open_positions:
                return no_trade(
                    "max_open_positions",
                    ["Open position already active; scanner will not add another."],
                    metrics=count_metrics,
                    campaign=campaign,
                )
            if trades_today >= campaign.max_trades_per_day:
                return no_trade(
                    "daily_trade_cap",
                    [f"Daily trade cap reached: {trades_today}/{campaign.max_trades_per_day}."],
                    metrics=count_metrics,
                    campaign=campaign,
                )
            if realized_today <= -campaign.max_daily_loss:
                lockout_line = f"Daily paper loss lockout active: {money(realized_today)} <= -{money(campaign.max_daily_loss)}."
                # Loss lockout is intentionally not silenced by quiet_no_change; still persist it.
                record_no_entry_decision(
                    config,
                    mode=run_context,
                    blocker="daily_loss_lockout",
                    reason=lockout_line,
                    metrics=count_metrics,
                    campaign=campaign,
                    cur=cur,
                )
                return lines + [lockout_line]
            underlying_ltp, underlying_meta, stale = get_quote(cur, config.underlying_symbol, config.quote_stale_seconds)
            if underlying_ltp is None or stale:
                message = f"BankNifty quote missing/stale for {config.underlying_symbol}; token/quote refresh needed."
                return no_trade(
                    "quote_stale",
                    [message],
                    metrics={**count_metrics, "underlying_symbol": config.underlying_symbol, "quote_stale": True, "underlying_ltp": str(underlying_ltp) if underlying_ltp is not None else None},
                    campaign=campaign,
                )
            moves, missing = get_constituent_moves(cur, config.constituents, config.quote_stale_seconds)
            signal = evaluate_constituent_led_direction(
                underlying_ltp=underlying_ltp,
                underlying_meta=underlying_meta,
                moves=moves,
                missing=missing,
                config=config,
            )
            if signal.direction is None:
                return no_trade(
                    "direction_unconfirmed",
                    [signal.reason],
                    reason=signal.reason,
                    metrics={**count_metrics, "signal_raw": signal.raw, "missing_constituents": missing},
                    campaign=campaign,
                )
            direction = signal.direction
            vwap_volume_decision: ConfirmationDecision | None = None
            if config.vwap_volume_confirmation_enabled:
                vwap_volume_decision = evaluate_vwap_volume_confirmation(
                    moves,
                    direction=direction,
                    min_confirming_top_movers=config.min_vwap_volume_confirming_top_movers,
                    rel_volume_threshold=config.relative_volume_threshold,
                )
                if not vwap_volume_decision.allowed:
                    return no_trade(
                        "vwap_volume",
                        [signal.reason, *vwap_volume_decision.reasons],
                        reason=" ".join(vwap_volume_decision.reasons),
                        metrics={"direction": direction, "guard_raw": vwap_volume_decision.raw},
                        campaign=campaign,
                    )
            weighted_vwap_decision = evaluate_weighted_vwap_side(
                moves,
                direction=direction,
                min_side_pct=config.weighted_vwap_side_pct,
            )
            if not weighted_vwap_decision.allowed:
                return no_trade(
                    "weighted_vwap",
                    [signal.reason, *weighted_vwap_decision.reasons],
                    reason=" ".join(weighted_vwap_decision.reasons),
                    metrics={"direction": direction, "guard_raw": weighted_vwap_decision.raw},
                    campaign=campaign,
                )
            index_rel_volume = get_relative_volume(cur, config.underlying_symbol, int_or_none(quote_raw_value(underlying_meta, "volume", "vol_traded_today")))
            day_high, day_low, adr10 = get_day_range_and_adr10(cur, config.underlying_symbol, resolution=config.structure_candle_resolution)
            lunch_decision = evaluate_lunch_chop_guard(
                now_local,
                day_high=day_high,
                day_low=day_low,
                adr10=adr10,
                index_rel_volume=index_rel_volume,
                window_start=config.lunch_window_start,
                window_end=config.lunch_window_end,
                min_range_vs_adr=config.lunch_min_day_range_vs_adr10,
                min_relvol=config.lunch_min_relvol,
            )
            if not lunch_decision.allowed:
                return no_trade(
                    "lunch_chop_guard",
                    [signal.reason, *lunch_decision.reasons],
                    reason=" ".join(lunch_decision.reasons),
                    metrics={
                        "direction": direction,
                        "index_rel_volume": None if index_rel_volume is None else str(index_rel_volume),
                        "day_high": None if day_high is None else str(day_high),
                        "day_low": None if day_low is None else str(day_low),
                        "adr10": None if adr10 is None else str(adr10),
                        "guard_raw": lunch_decision.raw,
                    },
                    campaign=campaign,
                )
            index_structure: IndexStructureSignal | None = None
            chop_decision: ConfirmationDecision | None = None
            if config.index_structure_confirmation_enabled:
                index_candles = get_recent_candles(
                    cur,
                    config.underlying_symbol,
                    limit=max(80, config.index_structure_lookback_candles + config.leg_lookback_candles + config.pullback_max_candles + 2),
                    resolution=config.structure_candle_resolution,
                )
                chop_decision = evaluate_chop_regime(
                    index_candles,
                    lookback_candles=config.chop_lookback_candles,
                    max_net_move_pct=config.chop_max_net_move_pct,
                    max_vwap_crosses=config.chop_max_vwap_crosses,
                )
                if not chop_decision.allowed:
                    return no_trade(
                        "chop_regime_guard",
                        [signal.reason, chop_decision.reasons[0]],
                        reason=chop_decision.reasons[0],
                        metrics={"direction": direction, "guard_raw": chop_decision.raw},
                        campaign=campaign,
                    )
                confluence_levels = confluence_levels_from_candles(
                    index_candles,
                    direction=direction,
                    structure_lookback=config.index_structure_lookback_candles,
                )
                index_structure = evaluate_pullback_continuation(
                    direction=direction,
                    candles=index_candles,
                    confluence_levels=confluence_levels,
                    breakout_buffer_pct=config.index_structure_breakout_buffer_pct,
                    level_hold_buffer_pct=config.pullback_level_hold_buffer_pct,
                    structure_stop_buffer_pct=config.index_structure_stop_buffer_pct,
                    leg_lookback_candles=config.leg_lookback_candles,
                    max_pullback_candles=config.pullback_max_candles,
                )
                if not index_structure.confirmed:
                    return no_trade(
                        "index_structure_unconfirmed",
                        [signal.reason, index_structure.reason],
                        reason=index_structure.reason,
                        metrics={"direction": direction, "structure_raw": index_structure.raw},
                        campaign=campaign,
                    )
                if index_structure.reference_level is not None:
                    cur.execute(
                        """
                        select realized_pnl, raw
                        from research.option_paper_trades
                        where campaign_id=%s
                          and option_type=%s
                          and status='closed'
                          and entry_time::date=current_date
                          and exit_reason in ('stop_loss', 'index_structure_stop')
                        """,
                        (campaign.campaign_id, direction),
                    )
                    for realized, raw_obj in cur.fetchall():
                        raw_trade = raw_obj if isinstance(raw_obj, dict) else {}
                        trade_ref = decimal_or_none(((raw_trade.get("index_structure") or {}) if isinstance(raw_trade.get("index_structure"), dict) else {}).get("reference_level"))
                        trade_risk = decimal_or_none(((raw_trade.get("realistic_risk_plan") or {}) if isinstance(raw_trade.get("realistic_risk_plan"), dict) else {}).get("risk_rupees")) or config.max_trade_loss
                        if trade_ref is not None and (trade_ref - index_structure.reference_level).copy_abs() <= Decimal("1") and decimal_or_none(realized) is not None and decimal_or_none(realized) <= -trade_risk * Decimal("0.95"):
                            burned = f"No trade: broken level {index_structure.reference_level} is burned for today after prior full -1R {direction} stop."
                            return no_trade(
                                "burned_structure_level",
                                [signal.reason, burned],
                                reason=burned,
                                metrics={
                                    "direction": direction,
                                    "reference_level": str(index_structure.reference_level),
                                    "trade_ref": str(trade_ref),
                                    "trade_risk": str(trade_risk),
                                },
                                campaign=campaign,
                            )
            jump_reasons = [
                reason
                for reason in (
                    major_constituent_jump_reason(
                        move,
                        direction=direction,
                        major_jump_threshold_pct=config.major_jump_threshold_pct,
                        rel_volume_threshold=config.relative_volume_threshold,
                    )
                    for move in moves
                )
                if reason is not None
            ]
            reason_parts = [signal.reason]
            if vwap_volume_decision is not None:
                reason_parts.extend(vwap_volume_decision.reasons)
            reason_parts.extend(weighted_vwap_decision.reasons)
            reason_parts.extend(lunch_decision.reasons)
            if chop_decision is not None:
                reason_parts.extend(chop_decision.reasons)
            if index_structure is not None:
                reason_parts.append(index_structure.reason)
            if jump_reasons:
                reason_parts.append("Jump reasons: " + " | ".join(j.summary for j in jump_reasons[:3]))
            reason = " ".join(reason_parts)
            contracts = get_contracts_from_db(cur, config)
            if not contracts:
                active = [c for c in fetch_fyers_banknifty_options() if c.expiry >= now_local.date()]
                upsert_contracts(cur, active)
                contracts = active
            candidates = select_directional_contract_candidates(contracts, direction=direction, underlying_ltp=underlying_ltp, today=now_local.date(), strike_step=config.strike_step)
            if not candidates:
                return no_trade(
                    "option_contract_unavailable",
                    [f"No trade: no ATM/first-OTM {direction} contract available around BankNifty {underlying_ltp}."],
                    metrics={"direction": direction, "underlying_ltp": str(underlying_ltp), "contracts_available": len(contracts)},
                    campaign=campaign,
                )
            contract = candidates[0]
    # Refresh selected option quotes outside the previous transaction so the new quotes are visible freshly.
    if refresh:
        warning = safe_refresh_quotes([c.symbol for c in candidates])
        if warning:
            refresh_warnings.append(warning)
        if config.realistic_risk_enabled:
            warning = safe_refresh_today_history([c.symbol for c in candidates], resolution=config.structure_candle_resolution)
            if warning:
                refresh_warnings.append(warning)
    with connect_db() as conn:
        with conn.cursor() as cur:
            audit_cur = cur
            campaign = current_campaign(cur, config)
            selected_state: dict[str, Any] | None = None
            rejection_reasons: list[str] = []
            # Fresh option-chain greeks/IV/OI per candidate, sourced once and reused
            # for both liquidity ranking and risk-filter enrichment. Empty when the
            # chain ingester is absent/stale, so everything below falls back to
            # today's quote-only behavior. atm_strike is the moneyness anchor so the
            # ATM/OTM1 tag stays correct even after liquidity reordering.
            atm_strike = nearest_strike(underlying_ltp, config.strike_step)
            chain_metrics_by_symbol: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                fields, stale = get_chain_metrics(cur, candidate.symbol, config.chain_stale_seconds)
                if fields and not stale:
                    chain_metrics_by_symbol[candidate.symbol] = fields
            if config.chain_selection_enabled:
                candidates = rank_chain_candidates(candidates, chain_metrics_by_symbol)
            # Option-chain context gate (PCR / IV regime / OI buildup). Advisory by
            # default — only vetoes the entry when a contradiction's block_* flag is
            # set and the summary is fresh. Recorded on the trade either way.
            chain_signal_decision: ChainSignalDecision | None = None
            if config.chain_signals.enabled:
                chain_summary, chain_summary_stale = get_chain_summary(cur, config.underlying, config.chain_stale_seconds)
                if chain_summary and not chain_summary_stale:
                    chain_signal_decision = evaluate_chain_signals(
                        direction=direction, summary=chain_summary, cfg=config.chain_signals
                    )
                    if not chain_signal_decision.allowed:
                        veto = "No trade: option-chain signal gate vetoed entry: " + "; ".join(chain_signal_decision.reasons)
                        return no_trade(
                            "option_chain_veto",
                            [veto],
                            reason=veto,
                            metrics={"direction": direction, "chain_raw": chain_signal_decision.raw},
                            campaign=campaign,
                        )
            for candidate in candidates:
                option_ltp, option_meta, option_stale = get_quote(cur, candidate.symbol, config.quote_stale_seconds)
                if option_ltp is None or option_stale:
                    rejection_reasons.append(f"{candidate.symbol}: option quote missing/stale")
                    continue
                # Merge chain greeks/IV/OI into the quote so the risk filter's
                # greeks/IV/OI guards can act on this candidate.
                chain_fields = chain_metrics_by_symbol.get(candidate.symbol)
                if chain_fields:
                    option_meta = {**option_meta, **chain_fields}
                if candidate.expiry == now_local.date() and now_local.time() >= dtime(13, 0) and (index_rel_volume is None or index_rel_volume < config.expiry_pm_min_relvol):
                    rejection_reasons.append(f"{candidate.symbol}: expiry-day PM rel-vol {index_rel_volume} below required {config.expiry_pm_min_relvol}")
                    continue
                risk_decision: RiskFilterDecision | None = None
                if config.risk_filter.enabled:
                    risk_decision = evaluate_option_risk_filters(
                        option_ltp=option_ltp,
                        option_meta=option_meta,
                        option_type=candidate.option_type,
                        risk_filter=config.risk_filter,
                    )
                    if not risk_decision.allowed:
                        rejection_reasons.append(f"{candidate.symbol}: risk filter rejected: {'; '.join(risk_decision.reasons)}")
                        continue
                risk_option_candles = get_recent_candles(
                    cur,
                    candidate.symbol,
                    limit=max(config.option_structure_lookback_candles, 3),
                    resolution=config.structure_candle_resolution,
                )
                if not option_recent_volume_ok(risk_option_candles, lookback=3):
                    rejection_reasons.append(f"{candidate.symbol}: last 3 option candles have zero/missing volume")
                    continue
                beta = estimate_option_index_slope(
                    get_recent_candles(cur, config.underlying_symbol, limit=max(6, config.beta_lookback_min), resolution="1"),
                    get_recent_candles(cur, candidate.symbol, limit=max(6, config.beta_lookback_min), resolution="1"),
                )
                is_atm = candidate.strike == atm_strike
                if beta is None or beta <= 0:
                    beta = config.beta_fallback_atm if is_atm else config.beta_fallback_otm1
                structure_stop_level = index_structure.stop_level if index_structure is not None else None
                if structure_stop_level is None:
                    rejection_reasons.append(f"{candidate.symbol}: pullback structure stop unavailable")
                    continue
                index_distance = abs(underlying_ltp - structure_stop_level)
                estimated_stop = floor_to_tick(option_ltp - (index_distance * beta), candidate.tick_size)
                lots, quantity, premium_value, risk_rupees = size_lots_by_risk(
                    entry_premium=option_ltp,
                    estimated_stop_premium=estimated_stop,
                    lot_size=candidate.lot_size,
                    max_trade_loss=config.max_trade_loss,
                    max_premium_exposure=config.max_premium_exposure,
                )
                if lots < 1:
                    rejection_reasons.append(f"{candidate.symbol}: structural risk > {money(config.max_trade_loss)} at 1 lot/exposure cap")
                    continue
                target = round_to_tick(option_ltp + ((option_ltp - estimated_stop) * config.target_r_multiple), candidate.tick_size)
                risk_plan = RealisticRiskPlan(
                    stop_premium=estimated_stop,
                    target_premium=target,
                    index_stop=structure_stop_level,
                    risk_points=(option_ltp - estimated_stop).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
                    risk_rupees=risk_rupees,
                    target_points=(target - option_ltp).quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
                    raw={
                        "basis": "pullback_index_structure_beta_mapped",
                        "index_stop": str(structure_stop_level),
                        "observed_option_index_slope": str(beta),
                        "candidate_rank": "ATM" if is_atm else "OTM1",
                    },
                )
                contract = candidate
                stop = risk_plan.stop_premium
                selected_state = {
                    "option_ltp": option_ltp,
                    "option_meta": option_meta,
                    "risk_decision": risk_decision,
                    "risk_plan": risk_plan,
                    "lots": lots,
                    "quantity": quantity,
                    "premium_value": premium_value,
                    "target": target,
                }
                break
            if selected_state is None:
                reason_text = "No trade: ATM and first-OTM candidates rejected: " + "; ".join(rejection_reasons)
                return no_trade(
                    "option_candidate_rejected",
                    [reason_text],
                    reason=reason_text,
                    metrics={"direction": direction, "rejection_reasons": rejection_reasons},
                    campaign=campaign,
                )
            option_ltp = selected_state["option_ltp"]
            option_meta = selected_state["option_meta"]
            risk_decision = selected_state["risk_decision"]
            risk_plan = selected_state["risk_plan"]
            lots = selected_state["lots"]
            quantity = selected_state["quantity"]
            premium_value = selected_state["premium_value"]
            target = selected_state["target"]
            stop = risk_plan.stop_premium
            if index_structure is not None and risk_plan.index_stop is not None:
                index_structure = IndexStructureSignal(
                    index_structure.confirmed,
                    index_structure.reason + f" Pullback index SL {risk_plan.index_stop}.",
                    risk_plan.index_stop,
                    index_structure.reference_level,
                    {**index_structure.raw, "pullback_index_stop": str(risk_plan.index_stop)},
                )
            if dry_run:
                target_text = money(target) if config.fixed_target_exit_enabled else "disabled — trailing runner active"
                dry_lines = [
                    f"Dry-run candidate long {contract.option_type}: {contract.symbol}",
                    f"Strategy card: {strategy_card.name} ({strategy_card.strategy_id})",
                    f"Reason: {reason}",
                    f"Entry premium: {money(option_ltp)} | Realistic option SL: {money(stop)} | Target: {target_text}",
                    f"Index structure SL: {index_structure.stop_level if index_structure and index_structure.stop_level is not None else 'n/a'} | Lots/qty: {lots}/{quantity} | Premium exposure: {money(premium_value)}",
                ]
                risk_line = risk_filter_summary_line(risk_decision)
                if risk_line:
                    dry_lines.append(risk_line)
                return lines + dry_lines + ["Dry-run only — no paper trade row inserted."]
            cur.execute(
                """
                insert into research.option_paper_trades(
                    campaign_id, symbol, underlying, underlying_symbol, option_type, expiry, strike, status,
                    signal_reason, underlying_entry, entry_premium, stop_premium, target_premium,
                    highest_premium, lots, lot_size, quantity, premium_value, strategy_version, raw
                ) values (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                returning option_trade_id
                """,
                (
                    campaign.campaign_id,
                    contract.symbol,
                    contract.underlying,
                    config.underlying_symbol,
                    contract.option_type,
                    contract.expiry,
                    contract.strike,
                    reason,
                    underlying_ltp,
                    option_ltp,
                    stop,
                    target,
                    option_ltp,
                    lots,
                    contract.lot_size,
                    quantity,
                    premium_value,
                    config.strategy_version,
                    json_dumps_safe({
                        "strategy_card": strategy_card_as_raw(strategy_card),
                        "underlying_quote": underlying_meta,
                        "option_quote": option_meta,
                        "constituent_signal": signal.raw,
                        "vwap_volume_confirmation": (
                            {
                                "allowed": vwap_volume_decision.allowed,
                                "reasons": vwap_volume_decision.reasons,
                                "confirmed_symbols": vwap_volume_decision.confirmed_symbols,
                                "raw": vwap_volume_decision.raw,
                            }
                            if vwap_volume_decision is not None
                            else {"enabled": False}
                        ),
                        "weighted_vwap_side_confirmation": {
                            "allowed": weighted_vwap_decision.allowed,
                            "reasons": weighted_vwap_decision.reasons,
                            "raw": weighted_vwap_decision.raw,
                        },
                        "lunch_chop_guard": {
                            "allowed": lunch_decision.allowed,
                            "reasons": lunch_decision.reasons,
                            "raw": lunch_decision.raw,
                        },
                        "chop_regime_guard": (
                            {
                                "allowed": chop_decision.allowed,
                                "reasons": chop_decision.reasons,
                                "raw": chop_decision.raw,
                            }
                            if chop_decision is not None
                            else {"enabled": False}
                        ),
                        "index_structure": (
                            {
                                "enabled": True,
                                "confirmed": index_structure.confirmed,
                                "reason": index_structure.reason,
                                "stop_level": None if index_structure.stop_level is None else str(index_structure.stop_level),
                                "reference_level": None if index_structure.reference_level is None else str(index_structure.reference_level),
                                "raw": index_structure.raw,
                            }
                            if index_structure is not None
                            else {"enabled": False}
                        ),
                        "constituent_jump_reasons": [
                            {
                                "symbol": jump.symbol,
                                "direction": jump.direction,
                                "pct_from_open": str(jump.pct_from_open),
                                "contribution": str(jump.contribution),
                                "vwap_confirmed": jump.vwap_confirmed,
                                "relative_volume_confirmed": jump.relative_volume_confirmed,
                                "summary": jump.summary,
                            }
                            for jump in jump_reasons
                        ],
                        "realistic_risk_plan": (
                            risk_plan.raw | {
                                "stop_premium": str(risk_plan.stop_premium),
                                "target_premium": str(risk_plan.target_premium),
                                "fixed_target_exit_enabled": config.fixed_target_exit_enabled,
                                "risk_points": str(risk_plan.risk_points),
                                "risk_rupees": str(risk_plan.risk_rupees),
                                "target_points": str(risk_plan.target_points),
                            }
                            if risk_plan is not None
                            else {"enabled": False}
                        ),
                        "risk_filter": (
                            {
                                "allowed": risk_decision.allowed,
                                "reasons": risk_decision.reasons,
                                "warnings": risk_decision.warnings,
                                "metrics": risk_decision.raw,
                            }
                            if risk_decision is not None
                            else {"enabled": False}
                        ),
                        "chain_signal": (
                            {
                                "allowed": chain_signal_decision.allowed,
                                "reasons": chain_signal_decision.reasons,
                                "warnings": chain_signal_decision.warnings,
                                "metrics": chain_signal_decision.raw,
                            }
                            if chain_signal_decision is not None
                            else {"enabled": config.chain_signals.enabled}
                        ),
                    }),
                ),
            )
            trade_id = int(cur.fetchone()[0])
            insert_event(cur, trade_id, "paper_option_opened", option_ltp, quantity, f"Opened {contract.symbol} long {contract.option_type}; premium {money(option_ltp)}; realistic SL {money(stop)}; target {money(target)}; no live order.")
            opened_lines = [
                f"Opened paper long {contract.option_type}: {contract.symbol}",
                f"Strategy card: {strategy_card.name} ({strategy_card.strategy_id})",
                f"Reason: {reason}",
                f"Entry premium: {money(option_ltp)} | Option safety SL: {money(stop)} | Target: {money(target)}",
                f"Index structure SL: {index_structure.stop_level if index_structure and index_structure.stop_level is not None else 'n/a'} | Lots/qty: {lots}/{quantity} | Premium exposure: {money(premium_value)}",
            ]
            risk_line = risk_filter_summary_line(risk_decision)
            if risk_line:
                opened_lines.append(risk_line)
            return lines + opened_lines


def monitor_open_options(config: CampaignConfig, *, refresh: bool = False, quiet_no_change: bool = False) -> list[str]:
    header = ["## BankNifty Options Paper Monitor", "Safety: paper only — no FYERS orders placed.", ""]
    with connect_db() as conn:
        with conn.cursor() as cur:
            campaign = current_campaign(cur, config)
            cur.execute(
                """
                select option_trade_id, symbol, option_type, entry_premium, stop_premium, target_premium,
                       quantity, highest_premium, entry_time, underlying_symbol, raw
                from research.option_paper_trades
                where campaign_id=%s and status='open'
                order by entry_time asc
                """,
                (campaign.campaign_id,),
            )
            trades = cur.fetchall()
    if not trades:
        return [] if quiet_no_change else header + ["No open BankNifty option paper trades."]
    symbols = sorted({str(row[1]) for row in trades} | {str(row[9] or config.underlying_symbol) for row in trades})
    action_lines: list[str] = []
    unchanged: list[str] = []
    if refresh:
        warning = safe_refresh_quotes(symbols)
        if warning:
            action_lines.append(f"- {warning}")
    force_exit_utc = combine_ist_time(now_ist().date(), config.force_exit_time).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    with connect_db() as conn:
        with conn.cursor() as cur:
            force_exit_claims = claim_force_exit_requests(cur, {int(row[0]) for row in trades})
            for trade_id, symbol, option_type, entry, stop, target, quantity, highest, entry_time, underlying_symbol, raw in trades:
                trade_raw = raw if isinstance(raw, dict) else {}
                index_structure_raw = trade_raw.get("index_structure") if isinstance(trade_raw.get("index_structure"), dict) else {}
                structure_stop = decimal_or_none(index_structure_raw.get("stop_level"))
                entry_dec = Decimal(str(entry))
                stop_dec = Decimal(str(stop))
                target_dec = Decimal(str(target))
                qty = int(quantity)
                ltp, quote_meta, stale = get_quote(cur, str(symbol), config.quote_stale_seconds)
                force_request_id = force_exit_claims.get(int(trade_id))
                if force_request_id is not None:
                    exit_premium, pnl = evaluate_manual_force_exit(entry_premium=entry_dec, ltp=ltp, quantity=qty)
                    quote_note = " Quote was missing/stale; closed at last stored premium." if (ltp is None or stale) else ""
                    cur.execute(
                        """
                        update research.option_paper_trades
                        set status='closed', exit_premium=%s, exit_time=now(), realized_pnl=%s,
                            exit_reason='manual_force_exit', updated_at=now()
                        where option_trade_id=%s
                        """,
                        (exit_premium, pnl, trade_id),
                    )
                    insert_event(
                        cur,
                        int(trade_id),
                        "paper_option_closed_manual_force_exit",
                        exit_premium,
                        qty,
                        f"Closed by operator force-exit from the dashboard; exit {money(exit_premium)}; paper P&L {money(pnl)}.{quote_note} No live order.",
                    )
                    cur.execute(
                        "update research.control_requests set status='applied', processed_at=now(), result_message=%s where request_id=%s",
                        (f"closed trade {trade_id} at {exit_premium}; paper P&L {pnl}; quote_stale={bool(ltp is None or stale)}", force_request_id),
                    )
                    action_lines.append(f"- {symbol}: closed by manual force-exit; exit {money(exit_premium)}; paper P&L {money(pnl)}")
                    continue
                if ltp is None or stale:
                    reason, exit_premium, pnl = evaluate_stale_quote_force_exit(
                        entry_premium=entry_dec,
                        quantity=qty,
                        now=now,
                        force_exit_utc=force_exit_utc,
                    )
                    if reason:
                        cur.execute(
                            """
                            update research.option_paper_trades
                            set status='closed', exit_premium=%s, exit_time=now(), realized_pnl=%s,
                                exit_reason=%s, updated_at=now()
                            where option_trade_id=%s
                            """,
                            (exit_premium, pnl, reason, trade_id),
                        )
                        insert_event(
                            cur,
                            int(trade_id),
                            f"paper_option_closed_{reason}",
                            exit_premium,
                            qty,
                            f"Closed by {reason} because quote was missing/stale at force-exit time; paper P&L {money(pnl)}; no live order.",
                        )
                        action_lines.append(f"- {symbol}: closed by {reason}; exit {money(exit_premium)}; paper P&L {money(pnl)}")
                    else:
                        unchanged.append(f"- {symbol}: quote missing/stale; cannot evaluate exit yet.")
                    continue
                index_ltp, index_meta, index_stale = get_quote(cur, str(underlying_symbol or config.underlying_symbol), config.quote_stale_seconds)
                if config.index_structure_confirmation_enabled and structure_stop is None and index_ltp is not None and not index_stale:
                    seed_candles = get_recent_candles(
                        cur,
                        str(underlying_symbol or config.underlying_symbol),
                        limit=config.index_structure_lookback_candles,
                        resolution=config.structure_candle_resolution,
                    )
                    if not seed_candles:
                        fallback_candle = quote_meta_as_candle(index_ltp, index_meta)
                        seed_candles = [fallback_candle] if fallback_candle is not None else []
                    if seed_candles:
                        if str(option_type) == "CE":
                            structure_stop = (candle_decimal(seed_candles[-1], "low") * (Decimal("1") - config.index_structure_stop_buffer_pct / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                        else:
                            structure_stop = (candle_decimal(seed_candles[-1], "high") * (Decimal("1") + config.index_structure_stop_buffer_pct / Decimal("100"))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                        index_structure_raw = dict(index_structure_raw)
                        index_structure_raw.update({
                            "enabled": True,
                            "confirmed": False,
                            "stop_level": str(structure_stop),
                            "seeded_for_legacy_open_trade": True,
                            "seeded_at": now.isoformat(),
                            "reason": "Seeded index-structure stop for trade opened before structure guard was enabled.",
                        })
                        trade_raw["index_structure"] = index_structure_raw
                        cur.execute(
                            "update research.option_paper_trades set raw=%s::jsonb, updated_at=now() where option_trade_id=%s",
                            (json_dumps_safe(trade_raw), trade_id),
                        )
                        insert_event(cur, int(trade_id), "paper_index_structure_stop_seeded", ltp, qty, f"Seeded index-structure stop {structure_stop} for legacy open paper trade; no live order.", {"index_stop": str(structure_stop)})
                        action_lines.append(f"- {symbol}: seeded index-structure stop {structure_stop} for existing open paper trade")
                if config.swing_trailing_enabled and structure_stop is not None and index_ltp is not None and not index_stale:
                    index_candles = get_recent_candles(
                        cur,
                        str(underlying_symbol or config.underlying_symbol),
                        limit=config.index_structure_lookback_candles,
                        resolution=config.structure_candle_resolution,
                    )
                    if not index_candles:
                        fallback_candle = quote_meta_as_candle(index_ltp, index_meta)
                        index_candles = [fallback_candle] if fallback_candle is not None else []
                    trailed_stop = compute_swing_trailing_stop(
                        direction=str(option_type),
                        current_stop=structure_stop,
                        candles=index_candles,
                        stop_buffer_pct=config.index_structure_stop_buffer_pct,
                    )
                    if trailed_stop is not None:
                        index_structure_raw = dict(index_structure_raw)
                        index_structure_raw.update({
                            "enabled": True,
                            "stop_level": str(trailed_stop),
                            "trailing_updated_at": now.isoformat(),
                            "trailing_reason": "swing_based_index_structure_trailing_stop",
                        })
                        trade_raw["index_structure"] = index_structure_raw
                        cur.execute(
                            "update research.option_paper_trades set raw=%s::jsonb, updated_at=now() where option_trade_id=%s",
                            (json_dumps_safe(trade_raw), trade_id),
                        )
                        insert_event(cur, int(trade_id), "paper_index_structure_stop_raised", ltp, qty, f"Raised index-structure stop to {trailed_stop} using latest BankNifty swing; no live order.", {"index_stop": str(trailed_stop)})
                        action_lines.append(f"- {symbol}: raised index-structure stop to {trailed_stop} from BankNifty swing")
                        structure_stop = trailed_stop
                index_reason, index_exit_premium, index_pnl = evaluate_index_structure_exit(
                    option_type=str(option_type),
                    index_ltp=None if index_stale else index_ltp,
                    structure_stop=structure_stop,
                    option_ltp=ltp,
                    entry_premium=entry_dec,
                    quantity=qty,
                )
                if index_reason:
                    cur.execute(
                        """
                        update research.option_paper_trades
                        set status='closed', exit_premium=%s, exit_time=now(), realized_pnl=%s,
                            exit_reason=%s, updated_at=now()
                        where option_trade_id=%s
                        """,
                        (index_exit_premium, index_pnl, index_reason, trade_id),
                    )
                    insert_event(cur, int(trade_id), f"paper_option_closed_{index_reason}", index_exit_premium, qty, f"Closed by BankNifty index-structure stop {structure_stop}; index {index_ltp}; paper P&L {money(index_pnl)}; no live order.", {"index_ltp": None if index_ltp is None else str(index_ltp), "index_stop": None if structure_stop is None else str(structure_stop)})
                    action_lines.append(f"- {symbol}: closed by index-structure stop {structure_stop}; option exit {money(index_exit_premium)}; paper P&L {money(index_pnl)}")
                    continue
                high_dec = max(Decimal(str(highest or entry)), ltp)
                if high_dec != Decimal(str(highest or entry)):
                    cur.execute("update research.option_paper_trades set highest_premium=%s, updated_at=now() where option_trade_id=%s", (high_dec, trade_id))
                risk_plan_raw = trade_raw.get("realistic_risk_plan") if isinstance(trade_raw.get("realistic_risk_plan"), dict) else {}
                risk_rupees = decimal_or_none(risk_plan_raw.get("risk_rupees"))
                if risk_rupees is None:
                    risk_rupees = ((entry_dec - stop_dec) * Decimal(qty)).copy_abs().quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                ratchet_stop = compute_mfe_ratchet_stop(
                    entry_dec,
                    high_dec,
                    qty,
                    risk_rupees=risk_rupees,
                    breakeven_at_r=config.breakeven_at_r,
                    ratchet_start_r=config.ratchet_start_r,
                    ratchet_giveback_pct=config.ratchet_giveback_pct,
                    ratchet_giveback_min_inr=config.ratchet_giveback_min_inr,
                    tick_size=config.option_tick_size,
                )
                if ratchet_stop is not None and ratchet_stop > stop_dec:
                    cur.execute("update research.option_paper_trades set stop_premium=%s, updated_at=now() where option_trade_id=%s", (ratchet_stop, trade_id))
                    insert_event(cur, int(trade_id), "paper_option_stop_raised", ratchet_stop, qty, f"Raised paper stop to {money(ratchet_stop)} using R-based MFE ratchet after best observed premium {money(high_dec)}; no live order.")
                    action_lines.append(f"- {symbol}: raised paper stop to {money(ratchet_stop)} using R-based MFE ratchet after best observed premium {money(high_dec)}")
                    stop_dec = ratchet_stop
                unrealized_now = ((ltp - entry_dec) * Decimal(qty)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                reference_level = decimal_or_none(index_structure_raw.get("reference_level"))
                momentum_gone = False
                if reference_level is not None and index_ltp is not None and not index_stale:
                    momentum_gone = index_ltp < reference_level if str(option_type) == "CE" else index_ltp > reference_level
                stagnation_reason = evaluate_stagnation_exit(
                    pnl=unrealized_now,
                    risk_rupees=risk_rupees,
                    now=now,
                    entry_time=entry_time,
                    stagnation_minutes=config.stagnation_minutes,
                    stagnation_min_r=config.stagnation_min_r,
                    momentum_gone=momentum_gone,
                )
                if stagnation_reason:
                    cur.execute(
                        """
                        update research.option_paper_trades
                        set status='closed', exit_premium=%s, exit_time=now(), realized_pnl=%s,
                            exit_reason=%s, updated_at=now()
                        where option_trade_id=%s
                        """,
                        (ltp, unrealized_now, stagnation_reason, trade_id),
                    )
                    insert_event(cur, int(trade_id), f"paper_option_closed_{stagnation_reason}", ltp, qty, f"Closed by stagnation rule after {config.stagnation_minutes}m; paper P&L {money(unrealized_now)}; no live order.")
                    action_lines.append(f"- {symbol}: closed by stagnation rule; exit {money(ltp)}; paper P&L {money(unrealized_now)}")
                    continue
                reason, exit_premium, pnl = evaluate_option_exit(
                    ltp,
                    entry_dec,
                    stop_dec,
                    target_dec,
                    qty,
                    now=now,
                    entry_time=entry_time,
                    force_exit_utc=force_exit_utc,
                    highest_premium=high_dec,
                    profit_lock_trigger=config.profit_lock_trigger,
                    profit_lock_step=config.profit_lock_step,
                    tick_size=config.option_tick_size,
                    target_exit_enabled=config.fixed_target_exit_enabled,
                )
                if reason:
                    cur.execute(
                        """
                        update research.option_paper_trades
                        set status='closed', exit_premium=%s, exit_time=now(), realized_pnl=%s,
                            exit_reason=%s, updated_at=now()
                        where option_trade_id=%s
                        """,
                        (exit_premium, pnl, reason, trade_id),
                    )
                    insert_event(cur, int(trade_id), f"paper_option_closed_{reason}", exit_premium, qty, f"Closed by {reason}; paper P&L {money(pnl)}; no live order.")
                    action_lines.append(f"- {symbol}: closed by {reason}; exit {money(exit_premium)}; paper P&L {money(pnl)}")
                else:
                    unrealized = ((ltp - entry_dec) * qty).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                    index_sl_text = f"; index SL {structure_stop}" if structure_stop is not None else ""
                    target_text = money(target_dec) if config.fixed_target_exit_enabled else "disabled — trailing runner active"
                    unchanged.append(f"- {symbol}: open {option_type}; LTP {money(ltp)}; paper P&L {money(unrealized)}; option safety SL {money(stop_dec)}; target {target_text}{index_sl_text}")
    if action_lines:
        return header + action_lines + ([] if quiet_no_change else [""] + unchanged)
    return [] if quiet_no_change else header + unchanged


def snapshot_report(config: CampaignConfig, *, output: Path | None = None, print_report: bool = False) -> list[str]:
    today = now_ist().date()
    with connect_db() as conn:
        with conn.cursor() as cur:
            campaign = current_campaign(cur, config)
            cur.execute(
                """
                select
                    coalesce(sum(case when status='closed' then realized_pnl else 0 end), 0) as realized_pnl,
                    coalesce(sum(case when status='open' and q.ltp is not null then (q.ltp - entry_premium) * quantity else 0 end), 0) as unrealized_pnl,
                    count(*) filter (where status='open') as open_count,
                    count(*) filter (where status='closed') as closed_count
                from research.option_paper_trades t
                left join market.quotes q on q.symbol=t.symbol
                where t.campaign_id=%s
                """,
                (campaign.campaign_id,),
            )
            realized, unrealized, open_count, closed_count = cur.fetchone()
            realized_dec = Decimal(str(realized or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            unrealized_dec = Decimal(str(unrealized or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            equity = (campaign.starting_capital + realized_dec + unrealized_dec).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            cur.execute(
                """
                insert into research.option_paper_daily_snapshots(
                    campaign_id, snapshot_date, starting_capital, realized_pnl, unrealized_pnl,
                    equity, open_positions, closed_positions, raw
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                on conflict(campaign_id, snapshot_date) do update set
                    starting_capital=excluded.starting_capital,
                    realized_pnl=excluded.realized_pnl,
                    unrealized_pnl=excluded.unrealized_pnl,
                    equity=excluded.equity,
                    open_positions=excluded.open_positions,
                    closed_positions=excluded.closed_positions,
                    raw=excluded.raw,
                    updated_at=now()
                """,
                (
                    campaign.campaign_id,
                    today,
                    campaign.starting_capital,
                    realized_dec,
                    unrealized_dec,
                    equity,
                    int(open_count or 0),
                    int(closed_count or 0),
                    json_dumps_safe({"campaign": campaign.name}),
                ),
            )
            cur.execute(
                """
                select symbol, option_type, status, entry_premium, exit_premium, realized_pnl, exit_reason, created_at
                from research.option_paper_trades
                where campaign_id=%s
                order by created_at desc
                limit 5
                """,
                (campaign.campaign_id,),
            )
            recent = cur.fetchall()
    lines = [
        "## BankNifty Options Paper Campaign",
        "Safety: paper only — no live orders placed.",
        "",
        f"Campaign: {config.campaign_name}",
        f"Starting capital: {money(campaign.starting_capital)}",
        f"Equity: {money(equity)}",
        f"Realized P&L: {money(realized_dec)}",
        f"Unrealized P&L: {money(unrealized_dec)}",
        f"Open / closed trades: {int(open_count or 0)} / {int(closed_count or 0)}",
    ]
    if recent:
        lines += ["", "Recent trades:"]
        for symbol, opt_type, status, entry, exit_premium, pnl, exit_reason, created_at in recent:
            lines.append(f"- {symbol} {opt_type} {status}: entry {money(Decimal(str(entry)))}; exit {money(Decimal(str(exit_premium))) if exit_premium is not None else 'n/a'}; P&L {money(Decimal(str(pnl))) if pnl is not None else 'n/a'}; reason {exit_reason or 'n/a'}")
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        lines.append(f"Report file: {output}")
    if print_report:
        print("\n".join(lines))
    return lines


def no_trade_report(config: CampaignConfig, *, trade_date: date | None = None) -> list[str]:
    """Daily no-entry summary from persisted quiet-scan decisions."""
    report_date = trade_date or now_ist().date()
    with connect_db() as conn:
        with conn.cursor() as cur:
            campaign = current_campaign(cur, config)
            cur.execute(
                """
                select blocker, count(*) as ticks, min(decision_time), max(decision_time)
                from research.option_paper_no_entry_decisions
                where campaign_id=%s and trade_date=%s
                group by blocker
                order by ticks desc, blocker
                """,
                (campaign.campaign_id, report_date),
            )
            counts = cur.fetchall()
            cur.execute(
                """
                select decision_time, blocker, reason
                from research.option_paper_no_entry_decisions
                where campaign_id=%s and trade_date=%s
                order by decision_time asc, decision_id asc
                limit 1
                """,
                (campaign.campaign_id, report_date),
            )
            first = cur.fetchone()
            cur.execute(
                """
                select decision_time, blocker, reason
                from research.option_paper_no_entry_decisions
                where campaign_id=%s and trade_date=%s
                order by decision_time desc, decision_id desc
                limit 1
                """,
                (campaign.campaign_id, report_date),
            )
            latest = cur.fetchone()
    total = sum(int(row[1] or 0) for row in counts)
    lines = [
        "## BankNifty No-Trade Decision Report",
        "Safety: paper-only audit report — no live orders placed.",
        "",
        f"Campaign: {config.campaign_name}",
        f"Date: {report_date.isoformat()}",
        f"Persisted no-entry ticks: {total}",
    ]
    if not counts:
        lines.append("No no-entry decisions were logged for this date.")
        return lines

    def _decision_line(prefix: str, row: tuple[Any, ...] | None) -> str:
        if not row:
            return f"{prefix}: n/a"
        when, blocker, reason = row
        when_text = when.isoformat() if hasattr(when, "isoformat") else str(when)
        return f"{prefix}: {when_text} — {blocker}: {reason}"

    lines += [
        "",
        _decision_line("First blocker", first),
        _decision_line("Latest blocker", latest),
        "",
        "Counts by blocker:",
    ]
    for blocker, ticks, first_seen, latest_seen in counts:
        first_text = first_seen.isoformat() if hasattr(first_seen, "isoformat") else str(first_seen)
        latest_text = latest_seen.isoformat() if hasattr(latest_seen, "isoformat") else str(latest_seen)
        lines.append(f"- {blocker}: {int(ticks)} tick(s), first {first_text}, latest {latest_text}")
    return lines


def candle_coverage_report(config: CampaignConfig) -> list[str]:
    """Read-only report of constituent 5-minute candle coverage for today."""
    resolution = config.structure_candle_resolution
    total = len(config.constituents)
    with connect_db() as conn:
        with conn.cursor() as cur:
            missing = get_missing_constituent_candle_coverage(cur, config.constituents, resolution=resolution)
    missing_symbols = {c.fyers_symbol for c in missing}
    covered = total - len(missing)
    lines = [
        "## BankNifty Constituent Candle Coverage Report",
        "Safety: read-only coverage check — no live orders placed.",
        "",
        f"Campaign: {config.campaign_name}",
        f"Date: {now_ist().date().isoformat()}",
        f"Resolution: {resolution}-minute",
        f"Constituents covered: {covered}/{total}",
        "",
    ]
    if not config.constituents:
        lines.append("No constituents configured.")
        return lines
    for constituent in config.constituents:
        status = "MISSING" if constituent.fyers_symbol in missing_symbols else "ok"
        lines.append(f"- {constituent.symbol} ({constituent.fyers_symbol}): {status}")
    if missing:
        lines += [
            "",
            f"Missing today's {resolution}-minute candles: "
            + ", ".join(c.fyers_symbol for c in missing),
        ]
    else:
        lines += ["", "All constituents have today's candle coverage."]
    return lines


def init_campaign(config: CampaignConfig) -> list[str]:
    apply_migrations()
    with connect_db() as conn:
        with conn.cursor() as cur:
            campaign = get_or_init_campaign(cur, config)
    refresh_lines = refresh_contract_master()
    report_path = PROJECT_ROOT / "reports" / f"banknifty_options_paper_snapshot_{now_ist().date().isoformat()}.md"
    report_lines = snapshot_report(config, output=report_path, print_report=False)
    return [
        "## BankNifty Options Paper Campaign Initialized",
        "Safety: long-options paper trading only — no live orders placed.",
        f"Campaign: {campaign.name}",
        f"Starting capital: {money(campaign.starting_capital)}",
        f"Max premium exposure/trade: {money(config.max_premium_exposure)}",
        f"Max daily paper loss: {money(config.max_daily_loss)}",
        f"Max paper loss/trade cap: {money(config.max_trade_loss) if config.max_trade_loss > 0 else 'disabled'}",
        (f"Risk plan: realistic ATR/structure enabled; fixed target {'enabled' if config.fixed_target_exit_enabled else 'disabled for trailing runner'} ({config.target_r_multiple}R capped at +{config.target_pct * 100}% when enabled)" if config.realistic_risk_enabled else f"Stop/target: -{config.stop_loss_pct * 100}% / +{config.target_pct * 100}%"),
        f"Profit lock: trigger {money(config.profit_lock_trigger)}, step {money(config.profit_lock_step)}" if config.profit_lock_trigger > 0 and config.profit_lock_step > 0 else "Profit lock: disabled",
        f"Pre-entry scan cadence: every {config.entry_scan_interval_minutes} minutes",
        f"Post-entry monitor cadence: {config.poll_interval_seconds}s while a paper trade is open",
        f"Open-position status updates: every {config.open_position_update_interval_minutes} minutes",
        "",
        *refresh_lines,
        "",
        *report_lines,
    ]


def tick(config: CampaignConfig, *, refresh: bool = True, quiet_no_change: bool = True, loop_seconds: int = 55) -> list[str]:
    started = time.monotonic()
    emitted: list[str] = []

    open_trade_exists = has_open_option_trade(config)
    tick_started_ist = now_ist()
    existing_open_position = open_trade_exists
    emit_open_position_update = existing_open_position and should_run_entry_scan(
        tick_started_ist,
        config.open_position_update_interval_minutes,
    )
    if not open_trade_exists:
        if not should_run_entry_scan(tick_started_ist, config.entry_scan_interval_minutes):
            # Off the entry-scan minute boundary: persist a best-effort audit row so
            # the daily report can account for every no-entry tick, then stay silent
            # (do not run a full scan or monitor pass, keep quiet cron output unchanged).
            record_no_entry_decision(
                config,
                mode="tick",
                blocker="entry_scan_interval_wait",
                reason=(
                    f"Off entry-scan boundary: minute {tick_started_ist.minute} not a multiple of "
                    f"{config.entry_scan_interval_minutes}; waiting for next scan boundary."
                ),
                metrics={
                    "entry_scan_interval_minutes": config.entry_scan_interval_minutes,
                    "tick_minute": tick_started_ist.minute,
                    "tick_time": tick_started_ist.strftime("%H:%M:%S"),
                },
            )
            return emitted
        scan_lines = scan_for_entry(config, refresh=refresh, quiet_no_change=quiet_no_change, run_context="tick")
        if scan_lines:
            emitted.extend(scan_lines)
        open_trade_exists = has_open_option_trade(config)
        if not open_trade_exists:
            return emitted

    first_monitor_pass = True
    while True:
        monitor_quiet = quiet_no_change and not (first_monitor_pass and emit_open_position_update)
        lines = monitor_open_options(config, refresh=refresh, quiet_no_change=monitor_quiet)
        first_monitor_pass = False
        if lines:
            if emitted:
                emitted.append("")
            emitted.extend(lines)
        if time.monotonic() - started + config.poll_interval_seconds > loop_seconds:
            break
        time.sleep(config.poll_interval_seconds)
    return emitted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=["init", "refresh-contracts", "scan", "monitor", "tick", "report", "no-trade-report", "candle-coverage-report"], required=True)
    parser.add_argument("--refresh-quotes", action="store_true", help="Read-only FYERS quote refresh before evaluating.")
    parser.add_argument("--quiet-no-change", action="store_true", help="Print nothing if no action occurred.")
    parser.add_argument("--dry-run", action="store_true", help="For scan mode, evaluate candidate without inserting a paper trade.")
    parser.add_argument("--loop-seconds", type=int, default=55, help="For tick mode, keep polling within this many seconds.")
    parser.add_argument("--date", help="For no-trade-report mode, IST trade date YYYY-MM-DD (default: today).")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.mode == "init":
        lines = init_campaign(config)
    elif args.mode == "refresh-contracts":
        lines = refresh_contract_master()
    elif args.mode == "scan":
        lines = scan_for_entry(config, refresh=args.refresh_quotes, quiet_no_change=args.quiet_no_change, dry_run=args.dry_run)
    elif args.mode == "monitor":
        lines = monitor_open_options(config, refresh=args.refresh_quotes, quiet_no_change=args.quiet_no_change)
    elif args.mode == "tick":
        lines = tick(config, refresh=args.refresh_quotes, quiet_no_change=args.quiet_no_change, loop_seconds=args.loop_seconds)
    elif args.mode == "no-trade-report":
        report_date = date.fromisoformat(args.date) if args.date else None
        lines = no_trade_report(config, trade_date=report_date)
    elif args.mode == "candle-coverage-report":
        lines = candle_coverage_report(config)
    else:
        output = PROJECT_ROOT / "reports" / f"banknifty_options_paper_snapshot_{now_ist().date().isoformat()}.md"
        lines = snapshot_report(config, output=output, print_report=False)
    if lines:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
