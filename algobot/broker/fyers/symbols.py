"""Fyers symbol-format helpers (pure regex, no data-layer imports).

Fyers NSE derivative symbology:

- Monthly option:  ``NSE:NIFTY26JUL24500CE``   (YY + MMM + strike + CE/PE)
- Weekly option:   ``NSE:NIFTY2670724500CE``   (YY + M + DD + strike + CE/PE,
  where M is the month code ``1``-``9`` for Jan-Sep, ``O``/``N``/``D`` for
  Oct/Nov/Dec)
- Future:          ``NSE:NIFTY26JULFUT``       (YY + MMM + FUT)
- Equity / index:  ``NSE:SBIN-EQ`` / ``NSE:NIFTY50-INDEX``
"""
from __future__ import annotations

import re

_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"

# Monthly option: root + YY + MMM + strike (optionally decimal) + CE/PE
_MONTHLY_OPT_RE = re.compile(
    rf"^(?:[A-Z]+:)?[A-Z][A-Z0-9&\-]*?\d{{2}}(?:{_MONTHS})\d+(?:\.\d+)?(?:CE|PE)$")

# Weekly option: root + YY + month-code (1-9/O/N/D) + DD + strike + CE/PE
_WEEKLY_OPT_RE = re.compile(
    r"^(?:[A-Z]+:)?[A-Z][A-Z&\-]*?\d{2}[1-9OND]\d{2}\d+(?:\.\d+)?(?:CE|PE)$")

# Future: root + YY + MMM + FUT
_FUTURE_RE = re.compile(
    rf"^(?:[A-Z]+:)?[A-Z][A-Z0-9&\-]*?\d{{2}}(?:{_MONTHS})FUT$")


def _is_cash(symbol: str) -> bool:
    """-EQ / -INDEX cash-segment symbols are never derivatives."""
    return symbol.endswith(("-EQ", "-INDEX"))


def is_option(symbol: str) -> bool:
    """True when ``symbol`` is an NSE option (weekly or monthly) in Fyers format."""
    s = symbol.strip().upper()
    if _is_cash(s):
        return False
    return bool(_MONTHLY_OPT_RE.match(s) or _WEEKLY_OPT_RE.match(s))


def is_future(symbol: str) -> bool:
    """True when ``symbol`` is an NSE futures contract in Fyers format."""
    s = symbol.strip().upper()
    if _is_cash(s):
        return False
    return bool(_FUTURE_RE.match(s))
