"""Regression tests for run_ingest transaction durability in
scripts/ingest_fyers_optionchain.py (same missing-commit bug class as
ingest_fyers_quotes.py / ingest_fyers_history.py).
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.ingest_fyers_optionchain as ioc


# --------------------------------------------------------------------------- #
# Fakes — no real network or DB.
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

    def fetchone(self):
        return (101,)


class FakeConn:
    def __init__(self, store: dict) -> None:
        self.store = store

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, exc_type, *rest) -> bool:
        if exc_type is not None and not self.store["committed"]:
            self.store["run"] = None  # rolled back
        return False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)

    def commit(self) -> None:
        self.store["committed"] = True


class FakeApi:
    def __init__(self, response=None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc

    def optionchain(self, data=None):
        if self._exc is not None:
            raise self._exc
        return self._response


def _wire(monkeypatch, store: dict, *, response=None, exc=None) -> None:
    monkeypatch.setattr(ioc, "fyers", lambda: FakeApi(response=response, exc=exc))
    monkeypatch.setattr(ioc, "connect_db", lambda: FakeConn(store))
    # The full chain payload is irrelevant to transaction semantics.
    monkeypatch.setattr(ioc, "ingest_underlying", lambda cur, sym, resp, ts: 1)


def _store() -> dict:
    return {"calls": [], "run": None, "committed": False}


# --------------------------------------------------------------------------- #
def test_success_records_run(monkeypatch):
    store = _store()
    _wire(monkeypatch, store, response={"s": "ok", "data": {}})

    ioc.run_ingest(["NSE:NIFTYBANK-INDEX"], strikecount=5)

    assert store["run"] is not None
    assert store["run"]["status"] == "success"


def test_non_ok_response_records_error_run(monkeypatch):
    store = _store()
    response = {"s": "error", "code": -16, "message": "Could not authenticate the user"}
    _wire(monkeypatch, store, response=response)

    with pytest.raises(RuntimeError):
        ioc.run_ingest(["NSE:NIFTYBANK-INDEX"], strikecount=5)

    # Error row committed so the failure survives the rollback: without the
    # commit the ~1/minute cron would leave zero trace of a failing morning.
    assert store["committed"] is True
    assert store["run"] is not None
    assert store["run"]["status"] == "error"


def test_exception_records_error_run(monkeypatch):
    store = _store()
    _wire(monkeypatch, store, exc=ConnectionError("network down"))

    with pytest.raises(ConnectionError):
        ioc.run_ingest(["NSE:NIFTYBANK-INDEX"], strikecount=5)

    assert store["committed"] is True
    assert store["run"] is not None
    assert store["run"]["status"] == "error"


def test_error_notes_redact_token_from_exception(monkeypatch):
    fake_credential_value = "SUPER" + "SECRET" + "TOKEN" + "1234567890"
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", fake_credential_value)
    store = _store()
    _wire(monkeypatch, store,
          exc=RuntimeError(f"GET https://api.fyers.in/?token={fake_credential_value} failed"))

    with pytest.raises(RuntimeError):
        ioc.run_ingest(["NSE:NIFTYBANK-INDEX"], strikecount=5)

    notes = store["run"]["notes"]
    assert fake_credential_value not in notes
    assert "<FYERS_ACCESS_TOKEN>" in notes
