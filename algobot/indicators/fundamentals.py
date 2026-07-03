"""Fundamentals provider and screening.

``CsvFundamentals`` reads ``config/fundamentals.csv`` — a SYNTHETIC PLACEHOLDER
data file with realistic-ballpark (but hand-written, NOT live) numbers for the
NIFTY50 heavyweights. Replace it with a real feed (Screener/Tijori/broker
fundamentals API) before using any fundamental screen in production.

Columns: pe, pb, roce, roe, de_ratio, dividend_yield, eps_growth,
revenue_growth, payout_ratio, promoter_pledge, mcap_cr.
"""
from __future__ import annotations

import operator
from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd

FUND_COLUMNS = [
    "pe",
    "pb",
    "roce",
    "roe",
    "de_ratio",
    "dividend_yield",
    "eps_growth",
    "revenue_growth",
    "payout_ratio",
    "promoter_pledge",
    "mcap_cr",
]

_OPS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _normalize_symbol(symbol: str) -> str:
    """Normalize broker notation to a bare ticker: 'NSE:SBIN-EQ' -> 'SBIN'."""
    s = symbol.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    for suffix in ("-EQ", "-BE", "-INDEX"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s


def _empty_frame() -> pd.DataFrame:
    """Empty, correctly-typed fundamentals frame."""
    return pd.DataFrame({c: pd.Series(dtype=float) for c in FUND_COLUMNS})


class FundamentalsProvider(ABC):
    """Source of per-symbol fundamental ratios."""

    @abstractmethod
    def get(self, symbols: list[str]) -> pd.DataFrame:
        """Return a frame indexed by the symbols AS PASSED with FUND_COLUMNS.

        Symbols without data get all-NaN rows so screens simply drop them
        (NaN never passes a filter).
        """


class CsvFundamentals(FundamentalsProvider):
    """CSV-backed fundamentals (placeholder data — see module docstring).

    Args:
        path: CSV path; relative paths are resolved against the repo root.
              A missing file yields an empty typed frame rather than an error.
    """

    def __init__(self, path: str = "config/fundamentals.csv") -> None:
        p = Path(path)
        self.path = p if p.is_absolute() else _REPO_ROOT / p
        self._data = self._load()

    def _load(self) -> pd.DataFrame:
        if not self.path.exists():
            return _empty_frame()
        raw = pd.read_csv(self.path)
        raw["symbol"] = raw["symbol"].map(_normalize_symbol)
        raw = raw.set_index("symbol")
        for col in FUND_COLUMNS:
            if col not in raw.columns:
                raw[col] = float("nan")
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
        return raw[FUND_COLUMNS]

    def get(self, symbols: list[str]) -> pd.DataFrame:
        normalized = [_normalize_symbol(s) for s in symbols]
        out = self._data.reindex(normalized)
        out.index = pd.Index(symbols, name="symbol")
        return out


def screen(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """Filter a fundamentals frame with ``{'pe': ('<', 14), ...}`` style rules.

    Supported operators: ``<, <=, >, >=, ==, !=``. Rows with NaN in a filtered
    column NEVER pass (including for ``!=``). Unknown columns/operators raise.
    """
    mask = pd.Series(True, index=df.index)
    for column, (op, value) in filters.items():
        if column not in df.columns:
            raise KeyError(f"screen: unknown column {column!r}")
        if op not in _OPS:
            raise ValueError(f"screen: unsupported operator {op!r}")
        col = df[column]
        mask &= _OPS[op](col, value) & col.notna()
    return df[mask]
