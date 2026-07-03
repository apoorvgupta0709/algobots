from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import ingest_fyers_history as history


def test_validate_ingest_args_accepts_valid_daily_range() -> None:
    history.validate_ingest_args("D", "2026-01-01", "2026-06-03")


def test_validate_ingest_args_rejects_bad_date_format() -> None:
    with pytest.raises(SystemExit, match="--from must be YYYY-MM-DD"):
        history.validate_ingest_args("D", "01-01-2026", "2026-06-03")


def test_validate_ingest_args_rejects_non_padded_date() -> None:
    with pytest.raises(SystemExit, match="--from must be YYYY-MM-DD"):
        history.validate_ingest_args("D", "2026-1-01", "2026-06-03")


def test_validate_ingest_args_rejects_reversed_dates() -> None:
    with pytest.raises(SystemExit, match="--from must be on or before --to"):
        history.validate_ingest_args("D", "2026-06-04", "2026-06-03")


def test_validate_ingest_args_rejects_unsupported_resolution() -> None:
    with pytest.raises(SystemExit, match="--resolution must be one of"):
        history.validate_ingest_args("BAD", "2026-01-01", "2026-06-03")
