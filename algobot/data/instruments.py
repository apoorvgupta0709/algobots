"""Fyers derivative symbol construction and parsing.

Formats (Fyers NSE F&O notation):

* monthly option:  ``NSE:{ROOT}{YY}{MMM}{STRIKE}{CE|PE}``  e.g. NSE:NIFTY26JUL24500CE
* weekly option:   ``NSE:{ROOT}{YY}{M}{DD}{STRIKE}{CE|PE}`` e.g. NSE:NIFTY2670724500CE
  where ``M`` is 1-9 for Jan-Sep and O/N/D for Oct/Nov/Dec. A weekly expiry
  that coincides with the monthly expiry uses the monthly format.
* future:          ``NSE:{ROOT}{YY}{MMM}FUT``

Whether an expiry date is "the monthly" is decided by
:mod:`algobot.data.expiries`.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from algobot.core.exceptions import DataError
from algobot.data.expiries import monthly_expiry

logger = logging.getLogger(__name__)

_MONTH_ABBR = ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_WEEKLY_MONTH_CODES = ["", "1", "2", "3", "4", "5", "6", "7", "8", "9", "O", "N", "D"]
_WEEKLY_CODE_TO_MONTH = {c: m for m, c in enumerate(_WEEKLY_MONTH_CODES) if c}

# Index spot symbol <-> derivative root.
_UNDERLYING_TO_ROOT = {
    "NIFTY50": "NIFTY",
    "NIFTYBANK": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "NIFTYNXT50": "NIFTYNXT50",
}
_ROOT_TO_UNDERLYING = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY": "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
    "NIFTYNXT50": "NSE:NIFTYNXT50-INDEX",
}

_MONTHLY_RE = re.compile(
    r"^(?P<root>[A-Z][A-Z0-9&-]*?)(?P<yy>\d{2})"
    r"(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"(?P<strike>\d+(?:\.\d+)?)$")
_WEEKLY_RE = re.compile(
    r"^(?P<root>[A-Z][A-Z0-9&-]*?)(?P<yy>\d{2})(?P<mc>[1-9OND])(?P<dd>\d{2})"
    r"(?P<strike>\d+(?:\.\d+)?)$")


def root_of(underlying: str) -> str:
    """Derivative root for a Fyers underlying symbol.

    "NSE:NIFTY50-INDEX" -> "NIFTY", "NSE:NIFTYBANK-INDEX" -> "BANKNIFTY",
    "NSE:SBIN-EQ" -> "SBIN".
    """
    name = underlying.upper().split(":", 1)[-1]
    for suffix in ("-INDEX", "-EQ"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return _UNDERLYING_TO_ROOT.get(name, name)


def underlying_of(root: str) -> str:
    """Inverse of :func:`root_of`: spot symbol for a derivative root."""
    root = root.upper()
    return _ROOT_TO_UNDERLYING.get(root, f"NSE:{root}-EQ")


def _fmt_strike(strike: int | float) -> str:
    value = float(strike)
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def option_symbol(root: str, expiry: dt.date, strike: int | float,
                  opt_type: str) -> str:
    """Fyers option symbol for ``root``/``expiry``/``strike``/``opt_type``.

    Uses the monthly format when ``expiry`` is that month's monthly expiry,
    otherwise the weekly format (month code + day).
    """
    root = root.upper()
    opt = opt_type.upper()
    if opt not in ("CE", "PE"):
        raise ValueError(f"opt_type must be CE or PE, got {opt_type!r}")
    if expiry == monthly_expiry(root, expiry.year, expiry.month):
        code = f"{expiry.year % 100:02d}{_MONTH_ABBR[expiry.month]}"
    else:
        code = (f"{expiry.year % 100:02d}"
                f"{_WEEKLY_MONTH_CODES[expiry.month]}{expiry.day:02d}")
    return f"NSE:{root}{code}{_fmt_strike(strike)}{opt}"


def future_symbol(root: str, expiry: dt.date) -> str:
    """Fyers futures symbol: ``NSE:{ROOT}{YY}{MMM}FUT``."""
    return f"NSE:{root.upper()}{expiry.year % 100:02d}{_MONTH_ABBR[expiry.month]}FUT"


def parse_option_symbol(symbol: str) -> dict:
    """Inverse of :func:`option_symbol` (both monthly and weekly formats).

    Returns ``{"root": str, "expiry": dt.date, "strike": float, "opt_type": str}``.
    Monthly symbols resolve the expiry through the holiday-adjusted calendar.
    """
    body = symbol.upper().split(":", 1)[-1]
    opt_type = body[-2:]
    if opt_type not in ("CE", "PE"):
        raise DataError(f"not an option symbol: {symbol!r}")
    core = body[:-2]

    m = _MONTHLY_RE.match(core)
    if m:
        year = 2000 + int(m["yy"])
        month = _MONTH_ABBR.index(m["mon"])
        expiry = monthly_expiry(m["root"], year, month)
        return {"root": m["root"], "expiry": expiry,
                "strike": float(m["strike"]), "opt_type": opt_type}

    m = _WEEKLY_RE.match(core)
    if m:
        expiry = dt.date(2000 + int(m["yy"]),
                         _WEEKLY_CODE_TO_MONTH[m["mc"]], int(m["dd"]))
        return {"root": m["root"], "expiry": expiry,
                "strike": float(m["strike"]), "opt_type": opt_type}

    raise DataError(f"cannot parse option symbol: {symbol!r}")
