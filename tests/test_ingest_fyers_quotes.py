from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.ingest_fyers_quotes as iq
import scripts.premarket_readiness_watchdog as wd
from scripts.premarket_readiness_watchdog import check_ingest_freshness, DATA


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
        elif "insert into market.quotes" in flat:
            self.store["upserts"] += 1

    def fetchone(self):
        # Only the run_id insert calls fetchone.
        return (101,)


class FakeConn:
    def __init__(self, store: dict) -> None:
        self.store = store

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, exc_type, *rest) -> bool:
        # Mimic psycopg3: rollback on exception. Anything not already committed
        # is discarded. We record whether the error row was committed first.
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

    def quotes(self, data=None):
        if self._exc is not None:
            raise self._exc
        return self._response


def _wire(monkeypatch, store: dict, *, response=None, exc=None) -> None:
    monkeypatch.setattr(iq, "fyers", lambda: FakeApi(response=response, exc=exc))
    monkeypatch.setattr(iq, "connect_db", lambda: FakeConn(store))


def _store() -> dict:
    return {"calls": [], "run": None, "upserts": 0, "committed": False}


# --------------------------------------------------------------------------- #
# success path is preserved
# --------------------------------------------------------------------------- #
def test_success_records_run_and_upserts(monkeypatch):
    store = _store()
    response = {
        "s": "ok",
        "d": [
            {"n": "NSE:SBIN-EQ", "v": {"lp": 100, "open_price": 99, "tt": 0}},
            {"n": "NSE:RELIANCE-EQ", "v": {"lp": 200, "open_price": 198}},
        ],
    }
    _wire(monkeypatch, store, response=response)

    iq.run_ingest(["NSE:SBIN-EQ", "NSE:RELIANCE-EQ"])

    assert store["run"] is not None
    assert store["run"]["status"] == "success"
    assert store["upserts"] == 2


# --------------------------------------------------------------------------- #
# the bug Codex flagged: a non-ok FYERS response must record a failed run
# --------------------------------------------------------------------------- #
def test_non_ok_response_records_error_run(monkeypatch):
    store = _store()
    response = {"s": "error", "code": -16, "message": "Could not authenticate the user"}
    _wire(monkeypatch, store, response=response)

    with pytest.raises(RuntimeError):
        iq.run_ingest(["NSE:SBIN-EQ"])

    # Run row was created (before the call) AND updated to error AND committed,
    # so the watchdog can see the latest failure.
    assert store["committed"] is True
    assert store["run"] is not None
    assert store["run"]["status"] == "error"
    assert store["upserts"] == 0


def test_exception_records_error_run(monkeypatch):
    store = _store()
    _wire(monkeypatch, store, exc=ConnectionError("network down"))

    with pytest.raises(ConnectionError):
        iq.run_ingest(["NSE:SBIN-EQ"])

    assert store["committed"] is True
    assert store["run"] is not None
    assert store["run"]["status"] == "error"


# --------------------------------------------------------------------------- #
# notes must never carry secrets
# --------------------------------------------------------------------------- #
def test_error_notes_redact_token_from_exception(monkeypatch):
    fake_credential_value = "SUPER" + "SECRET" + "TOKEN" + "1234567890"
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", fake_credential_value)
    store = _store()
    _wire(monkeypatch, store, exc=RuntimeError(f"GET https://api.fyers.in/?token={fake_credential_value} failed"))

    with pytest.raises(RuntimeError):
        iq.run_ingest(["NSE:SBIN-EQ"])

    notes = store["run"]["notes"]
    assert fake_credential_value not in notes
    assert "<FYERS_ACCESS_TOKEN>" in notes


def test_non_ok_note_omits_raw_payload(monkeypatch):
    fake_credential_value = "SUPER" + "SECRET" + "TOKEN" + "1234567890"
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", fake_credential_value)
    store = _store()
    # Fake credential value smuggled into both message and an extra raw field.
    response = {
        "s": "error",
        "code": -16,
        "message": f"auth failed token={fake_credential_value}",
        "raw_echo": {"access_token": fake_credential_value},
    }
    _wire(monkeypatch, store, response=response)

    with pytest.raises(RuntimeError):
        iq.run_ingest(["NSE:SBIN-EQ"])

    notes = store["run"]["notes"]
    assert fake_credential_value not in notes
    # Status/code summarized, raw payload field never copied verbatim.
    assert "s=error" in notes
    assert "code=-16" in notes
    assert "raw_echo" not in notes


def test_chp_never_stored_as_close(monkeypatch):
    # "chp" is FYERS change-PERCENT; storing it as a close price silently
    # corrupts market.quotes.close. With prev_close_price/close absent the
    # stored close must be NULL, never the percentage.
    store = _store()
    response = {"s": "ok", "d": [{"n": "NSE:SBIN-EQ", "v": {"lp": 100, "chp": 0.53}}]}
    _wire(monkeypatch, store, response=response)

    iq.run_ingest(["NSE:SBIN-EQ"])

    quote_inserts = [params for sql, params in store["calls"]
                     if "insert into market.quotes" in sql]
    assert len(quote_inserts) == 1
    # params: (symbol, ltp, open, high, low, close, volume, quote_time, raw)
    assert quote_inserts[0][5] is None


def test_redact_helper_caps_length():
    long = "x" * 5000
    assert len(iq._redact(long)) <= 520


# --------------------------------------------------------------------------- #
# watchdog blocks when the latest run is the error row this ingest writes
# --------------------------------------------------------------------------- #
def test_watchdog_blocks_on_latest_error_run():
    as_of = datetime(2026, 6, 16, 9, 25, tzinfo=wd.IST)
    # Latest run is an 'error' (what ingest_fyers_quotes now writes on failure),
    # even though an older success is still inside the freshness window.
    latest = {"status": "error", "finished_at": as_of - timedelta(seconds=30), "started_at": None}
    success = {"status": "success", "finished_at": as_of - timedelta(seconds=300), "started_at": None}
    r = check_ingest_freshness(latest, success, as_of, 600)
    assert not r.ok and r.severity == DATA
    assert "status=error" in r.detail
