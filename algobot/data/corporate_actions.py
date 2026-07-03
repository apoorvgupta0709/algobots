"""Minimal corporate-action back-adjustment for cached equity candles.

An action is ``{"symbol": str, "date": dt.date, "kind": "split"|"bonus",
"ratio": float}`` where ``ratio`` is the price divisor for bars strictly
BEFORE the ex-date: a 1:5 split has ratio 5, a 1:1 bonus has ratio 2.
Optionally loaded from ``config/corporate_actions.csv`` (columns:
symbol,date,kind,ratio); the file may not exist.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from algobot.core.config import CONFIG_DIR
from algobot.data.feed import TZ

logger = logging.getLogger(__name__)

_ACTIONS_FILE = CONFIG_DIR / "corporate_actions.csv"
_PRICE_COLS = ["open", "high", "low", "close"]


def load_actions(path: str | Path | None = None,
                 symbol: str | None = None) -> list[dict]:
    """Read actions from CSV; empty list when the file is absent.

    ``symbol`` filters to one instrument if given."""
    path = Path(path) if path else _ACTIONS_FILE
    if not path.exists():
        return []
    actions: list[dict] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            if symbol and row.get("symbol") != symbol:
                continue
            actions.append({
                "symbol": row.get("symbol", ""),
                "date": dt.date.fromisoformat(row["date"].strip()),
                "kind": (row.get("kind") or "split").strip().lower(),
                "ratio": float(row["ratio"]),
            })
    logger.debug("loaded %d corporate actions from %s", len(actions), path)
    return actions


def adjust(df: pd.DataFrame, actions: list[dict]) -> pd.DataFrame:
    """Back-adjust OHLC for splits/bonuses (non-mutating).

    Bars before each action's ex-date are divided by ``ratio`` (volume
    multiplied), making the series continuous at today's price scale."""
    out = df.copy()
    for action in actions:
        ratio = float(action["ratio"])
        if ratio <= 0 or ratio == 1.0:
            continue
        ex = pd.Timestamp(action["date"], tz=TZ)
        mask = out.index < ex
        if not mask.any():
            continue
        out.loc[mask, _PRICE_COLS] = out.loc[mask, _PRICE_COLS] / ratio
        if "volume" in out.columns:
            out.loc[mask, "volume"] = out.loc[mask, "volume"] * ratio
    return out
