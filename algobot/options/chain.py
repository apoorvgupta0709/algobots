"""Option chain abstraction: synthetic (BS-priced) or quote-backed.

A chain is a snapshot around a single underlying at time ``now``. Expiries are
passed to the query methods as dates; time-to-expiry is measured to the NSE
close, 15:30 IST, on the expiry date (ACT/365).
"""
from __future__ import annotations

import datetime as dt

from algobot.core.clock import IST, MARKET_CLOSE
from algobot.core.universes import strike_step
from algobot.options.pricing import (
    DEFAULT_RATE,
    YEAR_SECONDS,
    bs_greeks,
    bs_price,
    implied_vol,
)

SMILE_SLOPE = 0.15       # iv multiplier: * (1 + 0.15 * |K/S - 1|)
EXPIRED_T = 1e-6         # years; below this deltas degenerate to 0/1
_GRID_EACH_SIDE = 40     # search grid half-width (in strike steps)


class OptionChain:
    """Premium/IV/strike lookups for one underlying at one instant.

    Use the classmethod constructors:

    - :meth:`synthetic` — pure Black-Scholes chain with a mild smile,
      ``iv * (1 + 0.15 * |K/S - 1|)``.
    - :meth:`from_quotes` — real premiums where available, synthetic fallback
      for missing strikes. Quote keys may be full broker symbols or bare
      ``"<strike><CE|PE>"`` strings (lookup is suffix-based, e.g. a key ending
      in ``"24500CE"`` serves strike 24500 calls).
    """

    def __init__(self, underlying: str, spot: float, now: dt.datetime,
                 base_iv: float = 0.14, quotes: dict[str, float] | None = None,
                 r: float = DEFAULT_RATE) -> None:
        self.underlying = underlying
        self.spot = float(spot)
        if now.tzinfo is None:
            now = IST.localize(now)
        self.now = now.astimezone(IST)
        self.base_iv = float(base_iv)
        self.r = r
        self._quotes = dict(quotes or {})
        self._step = float(strike_step(underlying))

    # ------------------------------------------------------------- constructors
    @classmethod
    def synthetic(cls, underlying: str, spot: float, now: dt.datetime,
                  iv: float = 0.14) -> "OptionChain":
        """Fully synthetic BS-priced chain with a mild IV smile."""
        return cls(underlying, spot, now, base_iv=iv)

    @classmethod
    def from_quotes(cls, underlying: str, spot: float, now: dt.datetime,
                    quotes: dict[str, float], iv: float = 0.14) -> "OptionChain":
        """Quote-backed chain; strikes without a quote fall back to synthetic."""
        return cls(underlying, spot, now, base_iv=iv, quotes=quotes)

    # ------------------------------------------------------------------ helpers
    def _t_years(self, expiry: dt.date) -> float:
        """Years from ``now`` to 15:30 IST on the expiry date (floored at 0)."""
        if isinstance(expiry, dt.datetime):
            expiry = expiry.date()
        cutoff = IST.localize(dt.datetime.combine(expiry, MARKET_CLOSE))
        return max((cutoff - self.now).total_seconds(), 0.0) / YEAR_SECONDS

    def _quote_for(self, strike: float, opt_type: str) -> float | None:
        suffix = f"{int(round(strike))}{opt_type}"
        if suffix in self._quotes:
            return self._quotes[suffix]
        for key, px in self._quotes.items():
            if key.endswith(suffix):
                return px
        return None

    def _smile_iv(self, strike: float) -> float:
        return self.base_iv * (1.0 + SMILE_SLOPE * abs(strike / self.spot - 1.0))

    # ---------------------------------------------------------------------- api
    def atm_strike(self) -> float:
        """Spot rounded to the nearest exchange strike step."""
        return round(self.spot / self._step) * self._step

    def strikes(self, n_each_side: int = 10, expiry: dt.date | None = None) -> list[float]:
        """Strike grid: ATM +/- ``n_each_side`` steps (expiry kept for API parity)."""
        atm = self.atm_strike()
        return [atm + i * self._step for i in range(-n_each_side, n_each_side + 1)]

    def iv(self, strike: float, opt_type: str, expiry: dt.date) -> float:
        """Implied vol for a strike: backed out of the quote when one exists,
        otherwise the synthetic smile."""
        quote = self._quote_for(strike, opt_type)
        t = self._t_years(expiry)
        if quote is not None and t > EXPIRED_T:
            return implied_vol(quote, self.spot, strike, t, opt_type, self.r)
        return self._smile_iv(strike)

    def premium(self, strike: float, opt_type: str, expiry: dt.date) -> float:
        """Option premium: quoted if available, else BS with the smile IV
        (intrinsic at/after the 15:30 IST expiry cutoff)."""
        quote = self._quote_for(strike, opt_type)
        if quote is not None:
            return float(quote)
        t = self._t_years(expiry)
        return bs_price(self.spot, strike, t, self._smile_iv(strike), opt_type, self.r)

    def strike_by_delta(self, target_abs_delta: float, opt_type: str,
                        expiry: dt.date) -> float:
        """Grid strike whose |delta| is closest to ``target_abs_delta``.

        With time-to-expiry <= ~1e-6 years deltas degenerate to 0/1 and the
        search would walk off the end of the grid, so the ATM strike is
        returned instead.
        """
        t = self._t_years(expiry)
        if t <= EXPIRED_T:
            return self.atm_strike()
        best, best_err = self.atm_strike(), float("inf")
        for k in self.strikes(_GRID_EACH_SIDE):
            greeks = bs_greeks(self.spot, k, t, self.iv(k, opt_type, expiry),
                               opt_type, self.r)
            err = abs(abs(greeks["delta"]) - target_abs_delta)
            if err < best_err:
                best, best_err = k, err
        return best

    def strike_by_premium_pct(self, pct_of_spot: float, opt_type: str,
                              expiry: dt.date) -> float:
        """Grid strike whose premium is closest to ``pct_of_spot`` % of spot."""
        target = self.spot * pct_of_spot / 100.0
        best, best_err = self.atm_strike(), float("inf")
        for k in self.strikes(_GRID_EACH_SIDE):
            err = abs(self.premium(k, opt_type, expiry) - target)
            if err < best_err:
                best, best_err = k, err
        return best
