"""Fallback implementations of sibling subsystems used by the backtester.

The backtest engine and the reference strategies depend on modules owned by
other subsystems (``algobot.indicators.*``, ``algobot.options.*``,
``algobot.data.*``).  When those modules are absent (they are built in
parallel), this module provides reference implementations and can install
lightweight shim modules into ``sys.modules`` so the strategy plugins import
cleanly.  Real modules always win: shims are only installed on ImportError.
"""
from __future__ import annotations

import datetime as dt
import importlib
import logging
import math
import sys
import types
from statistics import NormalDist
from typing import Callable, Optional, Union

import numpy as np
import pandas as pd

from algobot.core.clock import IST
from algobot.core.enums import OptionType, Side
from algobot.core.models import ExpiryRule, OptionLeg, OptionStructure, StrikeRule
from algobot.core.universes import strike_step

log = logging.getLogger(__name__)

RISK_FREE = 0.065
DEFAULT_IV = 0.14
EXPIRY_WEEKDAY = 1        # Tuesday: NSE index weekly expiry (also the fallback rule)
_N = NormalDist()
_MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

IvSource = Union[float, Callable[[str, dt.datetime], float]]


# ------------------------------------------------------------------ indicators
def _atr_wilder(df: pd.DataFrame, n: int) -> pd.Series:
    prev_close = df.close.shift(1)
    tr = pd.concat([df.high - df.low,
                    (df.high - prev_close).abs(),
                    (df.low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Supertrend line and direction (+1 up / -1 down); columns [st, direction]."""
    hl2 = (df.high + df.low) / 2.0
    atr = _atr_wilder(df, period)
    ub = (hl2 + mult * atr).to_numpy(dtype=float)
    lb = (hl2 - mult * atr).to_numpy(dtype=float)
    close = df.close.to_numpy(dtype=float)
    n = len(df)
    st = np.full(n, np.nan)
    direction = np.ones(n, dtype=np.int64)
    fub, flb = ub.copy(), lb.copy()
    for i in range(1, n):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or close[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or close[i - 1] < flb[i - 1]) else flb[i - 1]
        if direction[i - 1] > 0:
            direction[i] = -1 if close[i] < flb[i] else 1
        else:
            direction[i] = 1 if close[i] > fub[i] else -1
        st[i] = flb[i] if direction[i] > 0 else fub[i]
    return pd.DataFrame({"st": st, "direction": direction}, index=df.index)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's Average Directional Index."""
    up = df.high.diff()
    dn = -df.low.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr = _atr_wilder(df, n)
    pdi = 100 * plus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / n, adjust=False).mean()


def realized_vol(close: pd.Series, n: int = 20) -> pd.Series:
    """Annualised realised volatility (fraction) of daily log returns."""
    return np.log(close).diff().rolling(n).std() * math.sqrt(252)


# ------------------------------------------------------------------ structures
def vertical_spread(underlying: str, option_type: OptionType, direction: str,
                    *, buy_rule: StrikeRule, sell_rule: StrikeRule,
                    expiry_rule: ExpiryRule, lots: int = 1) -> OptionStructure:
    """Two-leg vertical: buy ``buy_rule`` strike, sell ``sell_rule`` strike."""
    legs = [OptionLeg(Side.BUY, option_type, buy_rule, expiry_rule, lots),
            OptionLeg(Side.SELL, option_type, sell_rule, expiry_rule, lots)]
    return OptionStructure(name=f"vertical_{option_type.value.lower()}_{direction}",
                           underlying=underlying, legs=legs, net_direction=direction)


def iron_condor(underlying: str, short_delta: float = 0.20, wing_steps: int = 4,
                expiry_rule: Optional[ExpiryRule] = None, lots: int = 1) -> OptionStructure:
    """Short ~delta strangle with long wings ``wing_steps`` strikes further out."""
    er = expiry_rule or ExpiryRule.weekly()
    legs = [OptionLeg(Side.SELL, OptionType.CE, StrikeRule.delta(short_delta), er, lots),
            OptionLeg(Side.BUY, OptionType.CE, StrikeRule("rel", wing_steps), er, lots),
            OptionLeg(Side.SELL, OptionType.PE, StrikeRule.delta(short_delta), er, lots),
            OptionLeg(Side.BUY, OptionType.PE, StrikeRule("rel", wing_steps), er, lots)]
    return OptionStructure(name="iron_condor", underlying=underlying,
                           legs=legs, net_direction="credit")


# ------------------------------------------------------------------ expiries / symbols
def weekly_expiry(day: dt.date, n: int = 0) -> dt.date:
    """n-th weekly expiry (Tuesday) at/after ``day``."""
    base = day + dt.timedelta(days=(EXPIRY_WEEKDAY - day.weekday()) % 7)
    return base + dt.timedelta(weeks=n)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> dt.date:
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def monthly_expiry(day: dt.date, n: int = 0) -> dt.date:
    """n-th monthly expiry (last Tuesday of the month) at/after ``day``."""
    y, m = day.year, day.month
    exp = _last_weekday_of_month(y, m, EXPIRY_WEEKDAY)
    if exp < day:
        y, m = y + (m == 12), m % 12 + 1
        exp = _last_weekday_of_month(y, m, EXPIRY_WEEKDAY)
    for _ in range(n):
        y, m = y + (m == 12), m % 12 + 1
        exp = _last_weekday_of_month(y, m, EXPIRY_WEEKDAY)
    return exp


def is_expiry_day_fallback(root: str, day: dt.date) -> bool:  # noqa: ARG001
    """Fallback weekly-expiry test when algobot.data.expiries is absent."""
    return day.weekday() == EXPIRY_WEEKDAY


def root_of(symbol: str) -> str:
    """Derivative root of a Fyers spot/equity symbol (NIFTY50-INDEX -> NIFTY)."""
    s = symbol.split(":")[-1].upper()
    for suffix in ("-INDEX", "-EQ"):
        s = s.removesuffix(suffix)
    return s.replace("NIFTY50", "NIFTY").replace("NIFTYBANK", "BANKNIFTY")


def synth_option_symbol(root: str, expiry: dt.date, strike: float, opt_type: str) -> str:
    """Fyers weekly-format option symbol (encodes the exact expiry date)."""
    code = {10: "O", 11: "N", 12: "D"}.get(expiry.month, str(expiry.month))
    return f"NSE:{root}{expiry.year % 100:02d}{code}{expiry.day:02d}{int(round(strike))}{opt_type}"


def synth_future_symbol(root: str, expiry: dt.date) -> str:
    """Fyers monthly futures symbol."""
    return f"NSE:{root}{expiry.year % 100:02d}{_MONTH_ABBR[expiry.month - 1]}FUT"


def expiry_settlement_ts(expiry: dt.date) -> dt.datetime:
    """15:30 IST on the expiry date."""
    return IST.localize(dt.datetime.combine(expiry, dt.time(15, 30)))


def years_to_expiry(now: dt.datetime, expiry: dt.date) -> float:
    secs = (expiry_settlement_ts(expiry) - now).total_seconds()
    return max(secs, 0.0) / (365.0 * 86400.0)


# ------------------------------------------------------------------ pricing
def bs_price(spot: float, strike: float, t_years: float, iv: float,
             opt_type: str, r: float = RISK_FREE) -> float:
    """Black-Scholes European option price; intrinsic at/after expiry."""
    opt = str(getattr(opt_type, "value", opt_type)).upper()
    if opt == "FUT":
        return float(spot)
    intrinsic = max(spot - strike, 0.0) if opt == "CE" else max(strike - spot, 0.0)
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return intrinsic
    sq = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t_years) / sq
    d2 = d1 - sq
    if opt == "CE":
        return spot * _N.cdf(d1) - strike * math.exp(-r * t_years) * _N.cdf(d2)
    return strike * math.exp(-r * t_years) * _N.cdf(-d2) - spot * _N.cdf(-d1)


def strike_from_delta(spot: float, delta: float, opt_type: str, t_years: float,
                      iv: float, step: float, r: float = RISK_FREE) -> float:
    """Strike whose BS |delta| is closest to ``delta``, snapped to the step."""
    delta = min(max(abs(delta), 0.01), 0.99)
    if t_years <= 0 or iv <= 0:
        return round(spot / step) * step
    opt = str(getattr(opt_type, "value", opt_type)).upper()
    d1 = _N.inv_cdf(delta) if opt == "CE" else _N.inv_cdf(1.0 - delta)
    k = spot * math.exp((r + iv * iv / 2) * t_years - iv * math.sqrt(t_years) * d1)
    return round(k / step) * step


def _iv_of(iv_source: IvSource, symbol: str, now: dt.datetime) -> float:
    try:
        iv = iv_source(symbol, now) if callable(iv_source) else float(iv_source)
    except Exception:
        iv = DEFAULT_IV
    if not iv or not math.isfinite(iv) or iv <= 0:
        iv = DEFAULT_IV
    return min(max(iv, 0.05), 1.5)


# ------------------------------------------------------------------ option chain
class SyntheticOptionChain:
    """Minimal Black-Scholes option chain built from spot + realised vol."""

    def __init__(self, underlying: str, spot: float, iv: float = DEFAULT_IV,
                 now: Optional[dt.datetime] = None):
        self.underlying = underlying
        self.spot = float(spot)
        self.iv = float(iv) if iv and iv > 0 else DEFAULT_IV
        self.now = now or IST.localize(dt.datetime.now())
        self.step = float(strike_step(underlying))

    def atm_strike(self) -> float:
        return round(self.spot / self.step) * self.step

    def _t(self, expiry) -> float:
        if isinstance(expiry, str):
            expiry = dt.date.fromisoformat(expiry[:10])
        elif isinstance(expiry, dt.datetime):
            expiry = expiry.date()
        elif expiry is None:
            expiry = weekly_expiry(self.now.date())
        return years_to_expiry(self.now, expiry)

    def strike_by_delta(self, delta: float, opt_type: str, expiry=None) -> float:
        return strike_from_delta(self.spot, delta, opt_type, self._t(expiry),
                                 self.iv, self.step)

    def premium(self, strike: float, opt_type: str, expiry=None) -> float:
        return bs_price(self.spot, strike, self._t(expiry), self.iv, opt_type)


# ------------------------------------------------------------------ leg builder
class FallbackLegBuilder:
    """Resolves StrikeRule/ExpiryRule option legs into concrete synthetic symbols."""

    def __init__(self, iv_source: IvSource = DEFAULT_IV, risk_free: float = RISK_FREE):
        self.iv_source = iv_source
        self.risk_free = risk_free

    def resolve(self, structure: OptionStructure, spot: float,
                now: dt.datetime) -> OptionStructure:
        """Fill resolved_symbol/strike/expiry on every leg (in place)."""
        root = root_of(structure.underlying)
        step = float(strike_step(structure.underlying))
        iv = _iv_of(self.iv_source, structure.underlying, now)
        atm = round(spot / step) * step
        last_strike: dict[str, float] = {}

        for leg in structure.legs:
            kind, n = leg.expiry_rule.kind, leg.expiry_rule.n
            expiry = (weekly_expiry(now.date(), n) if kind == "weekly"
                      else monthly_expiry(now.date(), n))
            if expiry == now.date() and now.time() >= dt.time(15, 30):
                expiry = (weekly_expiry(now.date() + dt.timedelta(days=1), n)
                          if kind == "weekly"
                          else monthly_expiry(now.date() + dt.timedelta(days=1), n))
            if leg.option_type == OptionType.FUT:
                leg.resolved_strike = 0.0
                leg.resolved_expiry = expiry.isoformat()
                leg.resolved_symbol = synth_future_symbol(root, expiry)
                continue

            t = years_to_expiry(now, expiry)
            otype = leg.option_type.value
            strike = self._strike(leg.strike_rule, otype, spot, atm, step, t, iv,
                                  last_strike.get(otype, atm))
            last_strike[otype] = strike
            leg.resolved_strike = float(strike)
            leg.resolved_expiry = expiry.isoformat()
            leg.resolved_symbol = synth_option_symbol(root, expiry, strike, otype)
        return structure

    def _strike(self, rule: StrikeRule, otype: str, spot: float, atm: float,
                step: float, t: float, iv: float, prev: float) -> float:
        method, value = rule.method, rule.value
        if method == "atm":
            return atm + value * step
        if method == "absolute":
            return float(value)
        if method == "delta":
            return strike_from_delta(spot, value, otype, t, iv, step, self.risk_free)
        if method == "pct_otm":
            k = spot * (1 + value / 100) if otype == "CE" else spot * (1 - value / 100)
            return round(k / step) * step
        if method == "rel":       # value strikes further OTM than the previous leg
            return prev + value * step if otype == "CE" else prev - value * step
        if method == "premium_pct":
            target = value / 100.0 * spot
            candidates = [atm + i * step for i in range(-60, 61)]
            return min(candidates,
                       key=lambda k: abs(bs_price(spot, k, t, iv, otype,
                                                  self.risk_free) - target))
        log.warning("Unknown strike rule %r — using ATM", method)
        return atm


# ------------------------------------------------------------------ shim installer
_SHIMS: dict[str, dict[str, object]] = {
    "algobot.indicators.trend": {"supertrend": supertrend, "adx": adx},
    "algobot.indicators.volatility": {"realized_vol": realized_vol},
    "algobot.options.structures": {"vertical_spread": vertical_spread,
                                   "iron_condor": iron_condor},
}


def ensure_strategy_deps() -> list[str]:
    """Install shim modules for missing strategy dependencies.

    Returns the list of module names that were shimmed. Real modules, when
    present, are never overridden.
    """
    installed: list[str] = []
    for name, attrs in _SHIMS.items():
        try:
            importlib.import_module(name)
            continue
        except ImportError:
            pass
        mod = types.ModuleType(name)
        mod.__doc__ = "Backtest-subsystem fallback shim (algobot.backtest.compat)."
        for attr, obj in attrs.items():
            setattr(mod, attr, obj)
        sys.modules[name] = mod
        parent_name, _, tail = name.rpartition(".")
        try:
            setattr(importlib.import_module(parent_name), tail, mod)
        except ImportError:  # pragma: no cover - parent packages exist in-repo
            pass
        installed.append(name)
    if installed:
        log.info("Installed fallback shims for: %s", ", ".join(installed))
    return installed
