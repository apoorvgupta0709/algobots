"""NSE derivatives expiry calendar, holiday-adjusted (2026 regime).

Rules encoded here (reconfirm annually against NSE circulars):

* NIFTY is the only NSE index with a weekly expiry, and it expires TUESDAY.
* Monthly expiry for NIFTY / BANKNIFTY / FINNIFTY (and, by the same rule,
  stock derivatives) is the LAST TUESDAY of the month.
* BANKNIFTY and FINNIFTY have no weekly contracts: a "weekly" request for
  them resolves to the monthly expiry.
* An expiry that lands on a trading holiday moves to the PREVIOUS trading day.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
from itertools import islice
from typing import Iterator

from algobot.core.clock import is_trading_day, now_ist

logger = logging.getLogger(__name__)

EXPIRY_WEEKDAY = 1  # Tuesday (Monday=0)
WEEKLY_ROOTS = frozenset({"NIFTY"})  # only NSE weekly product in 2026


def _adjust(day: dt.date) -> dt.date:
    """Move an expiry falling on a holiday/weekend to the previous trading day."""
    while not is_trading_day(day):
        day -= dt.timedelta(days=1)
    return day


def monthly_expiry(root: str, year: int, month: int) -> dt.date:
    """Holiday-adjusted monthly expiry: last Tuesday of ``year``-``month``."""
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    nominal = last - dt.timedelta(days=(last.weekday() - EXPIRY_WEEKDAY) % 7)
    return _adjust(nominal)


def _weekly_expiries(on_date: dt.date) -> Iterator[dt.date]:
    """Adjusted weekly (Tuesday) expiries on or after ``on_date``, ascending."""
    nominal = on_date + dt.timedelta(days=(EXPIRY_WEEKDAY - on_date.weekday()) % 7)
    while True:
        adjusted = _adjust(nominal)
        if adjusted >= on_date:
            yield adjusted
        nominal += dt.timedelta(days=7)


def _monthly_expiries(root: str, on_date: dt.date) -> Iterator[dt.date]:
    """Adjusted monthly expiries on or after ``on_date``, ascending."""
    year, month = on_date.year, on_date.month
    while True:
        expiry = monthly_expiry(root, year, month)
        if expiry >= on_date:
            yield expiry
        month += 1
        if month > 12:
            month, year = 1, year + 1


def next_expiry(root: str, kind: str, n: int = 0,
                on_date: dt.date | None = None) -> dt.date:
    """The n-th expiry of ``kind`` on or after ``on_date`` (n=0 nearest).

    ``kind`` is "weekly" or "monthly". Roots without a weekly product
    (BANKNIFTY, FINNIFTY, equities) resolve weekly requests to the monthly
    calendar.
    """
    if kind not in ("weekly", "monthly"):
        raise ValueError(f"kind must be 'weekly' or 'monthly', got {kind!r}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    root = root.upper()
    on_date = on_date or now_ist().date()
    if kind == "weekly" and root in WEEKLY_ROOTS:
        seq = _weekly_expiries(on_date)
    else:
        seq = _monthly_expiries(root, on_date)
    return next(islice(seq, n, None))


def is_expiry_day(root: str, day: dt.date) -> bool:
    """True if ``day`` is an (adjusted) expiry day for ``root``'s contracts."""
    root = root.upper()
    kind = "weekly" if root in WEEKLY_ROOTS else "monthly"
    return next_expiry(root, kind, 0, on_date=day) == day


def days_to_expiry(root: str, kind: str, on_date: dt.date, n: int = 0) -> int:
    """Calendar days from ``on_date`` to the n-th ``kind`` expiry (0 on expiry day)."""
    return (next_expiry(root, kind, n, on_date=on_date) - on_date).days
