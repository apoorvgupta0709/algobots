"""Black-Scholes pricing, greeks, implied vol and synthetic premium series.

Conventions: continuous rate ``r`` (default 6.5% — approx. Indian repo+),
zero dividend yield, ACT/365 time in years. ``opt_type`` is "CE"/"PE".
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import norm

DEFAULT_RATE = 0.065
YEAR_DAYS = 365.0
YEAR_SECONDS = YEAR_DAYS * 24 * 3600
IV_FLOOR = 0.01   # 1%
IV_CAP = 3.00     # 300%


def _intrinsic(spot: float, strike: float, opt_type: str) -> float:
    if opt_type == "CE":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def _d1_d2(spot: float, strike: float, t_years: float, iv: float, r: float) -> tuple[float, float]:
    sig_sqrt_t = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / sig_sqrt_t
    return d1, d1 - sig_sqrt_t


def bs_price(spot: float, strike: float, t_years: float, iv: float, opt_type: str,
             r: float = DEFAULT_RATE) -> float:
    """Black-Scholes price of a European CE/PE; intrinsic value at ``t_years <= 0``."""
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return _intrinsic(spot, strike, opt_type)
    d1, d2 = _d1_d2(spot, strike, t_years, iv, r)
    df = math.exp(-r * t_years)
    if opt_type == "CE":
        return spot * norm.cdf(d1) - strike * df * norm.cdf(d2)
    return strike * df * norm.cdf(-d2) - spot * norm.cdf(-d1)


def bs_greeks(spot: float, strike: float, t_years: float, iv: float, opt_type: str,
              r: float = DEFAULT_RATE) -> dict:
    """Greeks: ``delta``, ``gamma``, ``theta_per_day`` (rupees/day, negative for
    longs) and ``vega_per_pct`` (rupees per 1 vol point).

    At/after expiry greeks degenerate: delta snaps to 0/±1 and the rest are 0.
    """
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        itm = _intrinsic(spot, strike, opt_type) > 0
        delta = (1.0 if itm else 0.0) if opt_type == "CE" else (-1.0 if itm else 0.0)
        return {"delta": delta, "gamma": 0.0, "theta_per_day": 0.0, "vega_per_pct": 0.0}
    d1, d2 = _d1_d2(spot, strike, t_years, iv, r)
    sqrt_t = math.sqrt(t_years)
    pdf_d1 = norm.pdf(d1)
    df = math.exp(-r * t_years)
    delta = norm.cdf(d1) if opt_type == "CE" else norm.cdf(d1) - 1.0
    gamma = pdf_d1 / (spot * iv * sqrt_t)
    if opt_type == "CE":
        theta = -spot * pdf_d1 * iv / (2 * sqrt_t) - r * strike * df * norm.cdf(d2)
    else:
        theta = -spot * pdf_d1 * iv / (2 * sqrt_t) + r * strike * df * norm.cdf(-d2)
    vega = spot * pdf_d1 * sqrt_t
    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "theta_per_day": float(theta / YEAR_DAYS),
        "vega_per_pct": float(vega / 100.0),
    }


def implied_vol(price: float, spot: float, strike: float, t_years: float, opt_type: str,
                r: float = DEFAULT_RATE) -> float:
    """Implied volatility by bisection, bounded to [1%, 300%].

    Prices at/below intrinsic clamp to the floor; prices above the model's
    300%-vol value clamp to the cap.
    """
    if t_years <= 0:
        return IV_FLOOR
    lo, hi = IV_FLOOR, IV_CAP
    if price <= bs_price(spot, strike, t_years, lo, opt_type, r):
        return lo
    if price >= bs_price(spot, strike, t_years, hi, opt_type, r):
        return hi
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t_years, mid, opt_type, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-8:
            break
    return 0.5 * (lo + hi)


def synthetic_premium_series(spot: pd.Series, strike: float, expiry_ts: pd.Timestamp,
                             opt_type: str, iv: float | pd.Series,
                             r: float = DEFAULT_RATE) -> pd.Series:
    """Vectorized BS premium along a tz-aware spot series with per-bar decay.

    Time to expiry is measured from each bar's timestamp to ``expiry_ts``
    (localized to the index tz when naive); bars at/after expiry get intrinsic.
    """
    idx = spot.index
    exp = pd.Timestamp(expiry_ts)
    if exp.tzinfo is None and idx.tz is not None:
        exp = exp.tz_localize(idx.tz)
    t = (exp - idx).total_seconds() / YEAR_SECONDS
    t = np.asarray(t, dtype=float)

    s = spot.to_numpy(dtype=float)
    sigma = (
        iv.reindex(idx).to_numpy(dtype=float)
        if isinstance(iv, pd.Series)
        else np.full(len(idx), float(iv))
    )
    k = float(strike)

    intrinsic = np.maximum(s - k, 0.0) if opt_type == "CE" else np.maximum(k - s, 0.0)
    live = (t > 0) & (sigma > 0) & (s > 0)
    out = intrinsic.copy()

    if live.any():
        tl, sl, sig = t[live], s[live], sigma[live]
        sqrt_t = np.sqrt(tl)
        d1 = (np.log(sl / k) + (r + 0.5 * sig**2) * tl) / (sig * sqrt_t)
        d2 = d1 - sig * sqrt_t
        df = np.exp(-r * tl)
        if opt_type == "CE":
            out[live] = sl * norm.cdf(d1) - k * df * norm.cdf(d2)
        else:
            out[live] = k * df * norm.cdf(-d2) - sl * norm.cdf(-d1)
    out[np.isnan(s)] = np.nan
    return pd.Series(out, index=idx, name=f"{int(k)}{opt_type}")
