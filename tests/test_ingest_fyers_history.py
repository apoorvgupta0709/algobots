"""Regression tests for run_ingest transaction durability in
scripts/ingest_fyers_history.py.

The bug class (already fixed once in ingest_fyers_quotes.py): the except
block wrote status='error' to market.ingestion_runs but never committed, so
psycopg's context-manager rollback discarded both the run row and every
candle already upserted for earlier symbols in the batch — after stdout had
already claimed they were stored.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.ingest_fyers_history as ih


# --------------------------------------------------------------------------- #
# Fakes — no real network or DB. Mirror just enough of psycopg / fyers.
# --------------------------------------------------------------------------- #
class FakeCursor:
    def __init__(self, store: dict) -> None:
        self.store = store

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        flat = " ".join(sql.split())
        self.store["calls"].append((flat, params))
        if "insert into market.ingestion_runs" in flat and "returning run_id" in flat:
            self.store["run"] = {"status": "running", "params": params, "notes": None}
        elif "update market.ingestion_runs" in flat:
            run = self.store["run"]
            if "status = 'success'" in flat:
                run["status"] = "success"
            elif "status = 'error'" in flat:
                run["status"] = "error"
                run["notes"] = params[0]
        elif "insert into market.candles" in flat:
            self.store["upserts"] += 1
            self.rowcount = 1

    rowcount = 1

    def fetchone(self):
        # Only the run_id insert calls fetchone.
        return (101,)


class FakeConn:
    def __init__(self, store: dict) -> None:
        self.store = store

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, exc_type, *rest) -> bool:
        # Mimic psycopg3: rollback on exception. Anything not committed is
        # discarded; anything committed earlier survives.
        if exc_type is not None and not self.store["committed"]:
            self.store["run"] = None
            self.store["durable_upserts"] = 0
        return False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)

    def commit(self) -> None:
        self.store["committed"] = True
        self.store["durable_upserts"] = self.store["upserts"]


class FakeApi:
    """history() answers from a per-call queue (one response per symbol)."""

    def __init__(self, responses=None, exc: Exception | None = None) -> None:
        self._responses = list(responses or [])
        self._exc = exc

    def history(self, data=None):
        if self._exc is not None:
            raise self._exc
        return self._responses.pop(0)


def _wire(monkeypatch, store: dict, *, responses=None, exc=None) -> None:
    monkeypatch.setattr(ih, "fyers", lambda: FakeApi(responses=responses, exc=exc))
    monkeypatch.setattr(ih, "connect_db", lambda: FakeConn(store))


def _store() -> dict:
    return {"calls": [], "run": None, "upserts": 0,
            "durable_upserts": 0, "committed": False}


_OK = {"s": "ok", "candles": [[1750041000, 100.0, 101.0, 99.0, 100.5, 1000],
                              [1750041300, 100.5, 102.0, 100.0, 101.5, 1200]]}
_ERR = {"s": "error", "code": -16, "message": "Could not authenticate the user"}

ARGS = dict(resolution="D", range_from="2026-01-01", range_to="2026-01-31",
            cont_flag="1")


# --------------------------------------------------------------------------- #
def test_success_records_run_and_upserts(monkeypatch):
    store = _store()
    _wire(monkeypatch, store, responses=[_OK])

    ih.run_ingest(["NSE:SBIN-EQ"], **ARGS)

    assert store["run"] is not None
    assert store["run"]["status"] == "success"
    assert store["upserts"] == 2


def test_non_ok_response_records_error_run(monkeypatch):
    store = _store()
    _wire(monkeypatch, store, responses=[_ERR])

    with pytest.raises(RuntimeError):
        ih.run_ingest(["NSE:SBIN-EQ"], **ARGS)

    # Error row committed so the failure survives the rollback.
    assert store["committed"] is True
    assert store["run"] is not None
    assert store["run"]["status"] == "error"


def test_exception_records_error_run(monkeypatch):
    store = _store()
    _wire(monkeypatch, store, exc=ConnectionError("network down"))

    with pytest.raises(ConnectionError):
        ih.run_ingest(["NSE:SBIN-EQ"], **ARGS)

    assert store["committed"] is True
    assert store["run"] is not None
    assert store["run"]["status"] == "error"


def test_earlier_symbol_candles_survive_later_failure(monkeypatch):
    # Symbol 1 succeeds (2 candles), symbol 2 fails: the 2 candles must have
    # been committed before the failure and must not be rolled back.
    store = _store()
    _wire(monkeypatch, store, responses=[_OK, _ERR])

    with pytest.raises(RuntimeError):
        ih.run_ingest(["NSE:SBIN-EQ", "NSE:RELIANCE-EQ"], **ARGS)

    assert store["durable_upserts"] == 2
    assert store["run"]["status"] == "error"


def test_error_notes_redact_token_from_exception(monkeypatch):
    fake_credential_value = "SUPER" + "SECRET" + "TOKEN" + "1234567890"
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", fake_credential_value)
    store = _store()
    _wire(monkeypatch, store,
          exc=RuntimeError(f"GET https://api.fyers.in/?token={fake_credential_value} failed"))

    with pytest.raises(RuntimeError):
        ih.run_ingest(["NSE:SBIN-EQ"], **ARGS)

    notes = store["run"]["notes"]
    assert fake_credential_value not in notes
    assert "<FYERS_ACCESS_TOKEN>" in notes
