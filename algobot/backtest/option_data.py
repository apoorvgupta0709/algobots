"""Option/futures pricing provider for backtests.

Prefers real cached candles (``algobot.data.cache.CandleCache``) when the data
subsystem is available; otherwise prices synthetically: futures mark at the
underlying, options via Black-Scholes on the parsed strike/expiry with a
configurable IV source. Tracks whether real or synthetic prices were used so
runs can be persisted with an honest ``data_source`` label.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

import pandas as pd

from algobot.backtest import compat
from algobot.backtest.compat import IvSource
from algobot.core.enums import Timeframe

log = logging.getLogger(__name__)

_MONTHS = {m: i + 1 for i, m in enumerate(compat._MONTH_ABBR)}
_CODE_MONTH = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
# Fyers monthly format: NSE:NIFTY25JUL24500CE
_RE_MONTHLY = re.compile(
    r"^(?:[A-Z]+:)?([A-Z][A-Z&-]*?)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"(\d+(?:\.\d+)?)(CE|PE)$")
# Fyers weekly format: NSE:NIFTY2570824500CE (YY, month code, DD, strike)
_RE_WEEKLY = re.compile(
    r"^(?:[A-Z]+:)?([A-Z][A-Z&-]*?)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")


def parse_option_symbol_fallback(symbol: str) -> Optional[tuple[str, dt.date, float, str]]:
    """Parse both Fyers option-symbol formats -> (root, expiry, strike, CE/PE)."""
    s = symbol.strip().upper()
    m = _RE_MONTHLY.match(s)
    if m:
        root, yy, mon, strike, opt = m.groups()
        expiry = compat._last_weekday_of_month(2000 + int(yy), _MONTHS[mon],
                                               compat.EXPIRY_WEEKDAY)
        return root, expiry, float(strike), opt
    m = _RE_WEEKLY.match(s)
    if m:
        root, yy, mcode, dd, strike, opt = m.groups()
        expiry = dt.date(2000 + int(yy), _CODE_MONTH[mcode], int(dd))
        return root, expiry, float(strike), opt
    return None


def _parse(symbol: str) -> Optional[tuple[str, dt.date, float, str]]:
    """Try the real instruments module first, then the local regex parser."""
    try:
        from algobot.data.instruments import parse_option_symbol  # lazy
        parsed = parse_option_symbol(symbol)
        if parsed is not None:
            get = parsed.get if isinstance(parsed, dict) else \
                (lambda k: getattr(parsed, k, None))
            root, expiry, strike = get("root"), get("expiry"), get("strike")
            opt = get("opt_type") or get("option_type")
            if isinstance(expiry, str):
                expiry = dt.date.fromisoformat(expiry[:10])
            elif isinstance(expiry, dt.datetime):
                expiry = expiry.date()
            if None not in (root, expiry, strike, opt):
                return (str(root), expiry, float(strike),
                        str(getattr(opt, "value", opt)))
    except Exception:
        pass
    return parse_option_symbol_fallback(symbol)


class OptionDataProvider:
    """Per-symbol premium lookup: real cached candles else synthetic pricing.

    ``iv_source`` is either a constant fraction (e.g. 0.14) or a callable
    ``(symbol, ts) -> iv``. ``data_source`` reports what was actually used.
    """

    def __init__(self, iv_source: IvSource = compat.DEFAULT_IV,
                 risk_free: float = compat.RISK_FREE,
                 staleness: pd.Timedelta = pd.Timedelta(days=1)):
        self.iv_source = iv_source
        self.risk_free = risk_free
        self.staleness = staleness
        self.used_real = False
        self.used_synthetic = False
        self._frames: dict[str, Optional[pd.DataFrame]] = {}
        self._warned: set[str] = set()

    # ---------------------------------------------------------------- public
    def premium(self, symbol: str, ts: pd.Timestamp, spot: float) -> float:
        """Price of one unit of ``symbol`` at ``ts`` given the underlying ``spot``."""
        real = self._real_close(symbol, ts)
        if real is not None:
            self.used_real = True
            return real
        self.used_synthetic = True
        return self._synthetic(symbol, ts, spot)

    @property
    def data_source(self) -> str:
        if self.used_real and self.used_synthetic:
            return "mixed"
        if self.used_real:
            return "real"
        return "synthetic"

    # ---------------------------------------------------------------- real
    def _frame(self, symbol: str) -> Optional[pd.DataFrame]:
        if symbol not in self._frames:
            df = None
            try:
                from algobot.data.cache import CandleCache  # lazy
                df = CandleCache().read(symbol, Timeframe.MIN5)
                if df is not None and (df.empty or "close" not in df.columns):
                    df = None
                if df is not None:
                    df = df.sort_index()
            except Exception:
                df = None
            self._frames[symbol] = df
        return self._frames[symbol]

    def _real_close(self, symbol: str, ts: pd.Timestamp) -> Optional[float]:
        df = self._frame(symbol)
        if df is None:
            return None
        try:
            idx = df.index
            if idx.tz is None and ts.tzinfo is not None:
                ts = ts.tz_localize(None)
            pos = idx.searchsorted(ts, side="right") - 1
            if pos < 0:
                return None
            bar_ts = idx[pos]
            if abs(pd.Timestamp(ts) - bar_ts) > self.staleness:
                return None            # nearest bar is stale
            return float(df["close"].iloc[pos])
        except Exception:
            log.debug("real premium lookup failed for %s", symbol, exc_info=True)
            return None

    # ---------------------------------------------------------------- synthetic
    def _synthetic(self, symbol: str, ts: pd.Timestamp, spot: float) -> float:
        if symbol.upper().endswith("FUT"):
            return round(float(spot), 2)      # futures mark at the underlying
        parsed = _parse(symbol)
        if parsed is None:
            if symbol not in self._warned:
                self._warned.add(symbol)
                log.warning("Cannot parse option symbol %r — marking at underlying", symbol)
            return round(float(spot), 2)
        _root, expiry, strike, opt = parsed
        t_years = compat.years_to_expiry(ts.to_pydatetime(), expiry)
        iv = compat._iv_of(self.iv_source, symbol, ts.to_pydatetime())
        # Apply the SAME volatility smile the chain uses to SELECT strikes, so
        # a leg is priced on the curve it was chosen from (OTM wings were
        # otherwise priced with a flatter IV than they were picked at).
        if spot > 0:
            try:
                from algobot.options.chain import SMILE_SLOPE
            except Exception:
                SMILE_SLOPE = 0.15
            iv = iv * (1.0 + SMILE_SLOPE * abs(strike / spot - 1.0))
        try:
            from algobot.options.pricing import bs_price  # lazy: real module first
            px = float(bs_price(spot, strike, t_years, iv, opt))
        except Exception:
            px = compat.bs_price(spot, strike, t_years, iv, opt, self.risk_free)
        return max(round(px / 0.05) * 0.05, 0.05) if t_years > 0 else round(max(px, 0.0), 2)
