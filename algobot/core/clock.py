"""IST market-clock utilities: sessions, holidays, open/close checks."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

import pytz
import yaml

IST = pytz.timezone("Asia/Kolkata")

MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
SQUAREOFF_START = dt.time(15, 15)   # intraday flatten window start
FIRST_SCAN = dt.time(9, 20)         # let the first 5-min candle close

_HOLIDAY_FILE = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


@lru_cache(maxsize=1)
def holidays() -> frozenset[dt.date]:
    if not _HOLIDAY_FILE.exists():
        return frozenset()
    raw = yaml.safe_load(_HOLIDAY_FILE.read_text()) or {}
    days: set[dt.date] = set()
    for _, datelist in (raw.get("holidays") or {}).items():
        for d in datelist or []:
            days.add(d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)))
    return frozenset(days)


def is_trading_day(day: dt.date | None = None) -> bool:
    day = day or now_ist().date()
    return day.weekday() < 5 and day not in holidays()


def is_market_open(ts: dt.datetime | None = None) -> bool:
    ts = ts or now_ist()
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    ts = ts.astimezone(IST)
    return is_trading_day(ts.date()) and MARKET_OPEN <= ts.time() < MARKET_CLOSE


def next_trading_day(day: dt.date | None = None) -> dt.date:
    day = (day or now_ist().date()) + dt.timedelta(days=1)
    while not is_trading_day(day):
        day += dt.timedelta(days=1)
    return day


def prev_trading_day(day: dt.date | None = None) -> dt.date:
    day = (day or now_ist().date()) - dt.timedelta(days=1)
    while not is_trading_day(day):
        day -= dt.timedelta(days=1)
    return day


def session_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    open_dt = IST.localize(dt.datetime.combine(day, MARKET_OPEN))
    close_dt = IST.localize(dt.datetime.combine(day, MARKET_CLOSE))
    return open_dt, close_dt


def week_start(day: dt.date | None = None) -> dt.date:
    """Monday of the given date's week (for weekly loss caps)."""
    day = day or now_ist().date()
    return day - dt.timedelta(days=day.weekday())
