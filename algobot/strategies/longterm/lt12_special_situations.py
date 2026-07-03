"""3.12 Special Situations — operator-fed corporate-event basket.

Return streams driven by corporate events, not market direction: a basket
across buybacks (tender arbitrage), demergers, open offers, delisting
attempts, promoter-buying clusters and index-inclusion flows. Size each
situation SMALL — the edge is the process, not any one deal.

OPERATOR-FED DESIGN (read this before enabling)
-----------------------------------------------
There is NO machine-readable feed for Indian corporate events on this
platform. The operator curates the basket by hand:

* preferred: pass a list of event dicts via ``ctx.extras["special_situations"]``;
* fallback: rows are loaded once (in ``__init__``) from
  ``config/special_situations.csv``. A missing file degrades to an empty
  basket — the strategy then emits nothing.

CSV schema (one row per situation)::

    symbol,event_type,entry_by,exit_by,notes
    NSE:ITC-EQ,demerger,2024-10-31,2025-01-06,record-date play

* ``symbol``      — Fyers notation, e.g. ``NSE:ITC-EQ``
* ``event_type``  — buyback | demerger | open_offer | delisting |
                    promoter_buying | index_inclusion
* ``entry_by``    — ISO date; only enter on/before this date
* ``exit_by``     — ISO date; the position is time-boxed and exited once
                    the scan date reaches this
* ``notes``       — free text, echoed into the signal reason (journal)

The SHIPPED ``config/special_situations.csv`` is a clearly-synthetic SAMPLE
with PAST dates — it is inert and nothing auto-trades until the operator
replaces it with live situations.

Edge: event-driven P&L has low correlation to market beta; discipline of
small, time-boxed, pre-committed positions across many deals is the edge.
Regime: none required — the basket is (mostly) direction-agnostic.
Risk: deal breaks (offer withdrawn, scheme rejected), regulatory delay
stretching the timeline past the exit window, and optimistic
acceptance-ratio maths on tenders. Each entry carries a deal-break stop
(default 8% below entry close) and a hard time-box.

India note: SEBI's tender-route buyback rules reserve 15% of the offer for
small shareholders (holdings up to Rs 2 lakh), which has historically
favoured retail with far better acceptance ratios than the general
category — but read each offer document and the company's ratio history
before assuming the entitlement maths.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from algobot.core import universes
from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta

_REPO_ROOT = Path(__file__).resolve().parents[3]

EVENT_TYPES = {
    "buyback", "demerger", "open_offer", "delisting",
    "promoter_buying", "index_inclusion",
}


def _parse_iso_date(value) -> Optional[date]:
    """ISO string / date / datetime -> date; anything unparseable -> None."""
    if value is None:
        return None
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    if hasattr(value, "date") and callable(value.date):   # datetime-like
        return value.date()
    try:
        return date.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None


def _normalize_event(row: dict) -> Optional[dict]:
    """Validate one raw event row (CSV or extras). Malformed rows -> None."""
    symbol = str(row.get("symbol", "") or "").strip()
    event_type = str(row.get("event_type", "") or "").strip().lower()
    entry_by = _parse_iso_date(row.get("entry_by"))
    exit_by = _parse_iso_date(row.get("exit_by"))
    if not symbol or event_type not in EVENT_TYPES:
        return None
    if entry_by is None or exit_by is None:
        return None
    return {
        "symbol": symbol,
        "event_type": event_type,
        "entry_by": entry_by,
        "exit_by": exit_by,
        "notes": str(row.get("notes", "") or "").strip(),
    }


class SpecialSituationsStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt12_special_situations",
        name="Special Situations Event Basket",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],   # data availability; actual basket is operator-fed
        warmup_bars=5,
        params={
            "per_situation_weight": 0.15,   # size each situation SMALL
            "situation_stop_pct": 8.0,      # deal-break protection below entry close
            "max_new_entries": 2,           # per scan
            "events_csv": "config/special_situations.csv",
        },
        capital_required=200_000,
        max_positions=5,
        max_trades_per_day=3,
        intraday_squareoff=False,
        description=("Operator-fed basket of corporate-event situations (buybacks, "
                     "demergers, open offers, delistings, promoter buying, index "
                     "inclusions) read from config/special_situations.csv or "
                     "ctx.extras. Small, time-boxed positions with deal-break stops; "
                     "the edge is the process, not any one deal."),
    )

    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        # Constructor I/O is allowed (signal-time I/O is not). Missing file -> [].
        self._csv_events = self._load_events_csv(self.params["events_csv"])

    @staticmethod
    def _load_events_csv(path: str) -> list[dict]:
        p = Path(path)
        if not p.is_absolute():
            p = _REPO_ROOT / p
        if not p.exists():
            return []
        with p.open(newline="") as fh:
            rows = [_normalize_event(row) for row in csv.DictReader(fh)]
        return [r for r in rows if r is not None]

    def _events(self, ctx: StrategyContext) -> list[dict]:
        """Operator events: prefer ctx.extras['special_situations'], else the CSV."""
        raw = ctx.extras.get("special_situations")
        if raw is None:
            return self._csv_events
        normalized = [_normalize_event(row) for row in raw]
        return [r for r in normalized if r is not None]

    def universe(self, ctx: StrategyContext) -> list[str]:
        """Event symbols that fall inside the NIFTY50 data universe, deduped."""
        allowed = set(universes.resolve(self.meta.instruments))
        out: list[str] = []
        for ev in self._events(ctx):
            sym = ev["symbol"]
            if sym in allowed and sym not in out:
                out.append(sym)
        return out

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        p = self.params
        today = ctx.now.date()
        open_syms = {pos.symbol for pos in ctx.open_positions}
        acted: set[str] = set()          # one signal per symbol per scan
        new_entries = 0

        for ev in self._events(ctx):
            sym = ev["symbol"]
            df = data.get(sym)
            if df is None or len(df) < self.meta.warmup_bars or sym in acted:
                continue
            close = float(df.close.iloc[-1])

            if sym in open_syms:
                # time-box: the event window has closed
                if today >= ev["exit_by"]:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason=f"{ev['event_type']}: event window closed"))
                    acted.add(sym)
                continue

            # entry: only on/before entry_by, small weight, deal-break stop
            if (today <= ev["entry_by"]
                    and new_entries < int(p["max_new_entries"])
                    and len(open_syms) + new_entries < self.meta.max_positions):
                stop = close * (1.0 - float(p["situation_stop_pct"]) / 100.0)
                note = f" — {ev['notes']}" if ev["notes"] else ""
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop,
                    size_hint=SizeHint(weight=float(p["per_situation_weight"])),
                    product_type=ProductType.CNC,
                    reason=f"{ev['event_type']}{note}"))
                acted.add(sym)
                new_entries += 1
        return signals
