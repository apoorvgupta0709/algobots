"""API service tests: REST reads, control actions and the Hermes query queue.

No network, no background thread: a throwaway sqlite DATABASE_URL is set
BEFORE any algobot import, the app is built with start_worker=False and the
queue is drained synchronously via QueryWorker.process_once().
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="algobot-api-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"
os.environ["ALGOBOT_API_DISABLE_WORKER"] = "1"

import pytest

from algobot.core import config as config_mod

config_mod.settings.cache_clear()

from algobot.persistence import db as db_mod

db_mod.get_engine.cache_clear()
db_mod.get_sessionmaker.cache_clear()

from fastapi.testclient import TestClient

from algobot.api.main import create_app
from algobot.api.query_worker import QueryWorker
from algobot.core.clock import now_ist
from algobot.persistence.db import session_scope
from algobot.persistence.schema import (
    BacktestRunRow,
    EquitySnapshotRow,
    EventLogRow,
    GateStatusRow,
    PositionRow,
    RiskStateRow,
    StrategyRow,
    TradeRow,
)

LIVE_SID = "id01_orb"
PAPER_SID = "sw04_supertrend_adx"

NOW = now_ist().replace(tzinfo=None)          # DB datetimes are naive IST
TODAY = NOW.date()
OLD = NOW - dt.timedelta(days=40)             # before today/week/month windows


def _engine_lifecycle_importable() -> bool:
    """Promote/demote behaviour depends on the parallel engine workstream."""
    try:
        importlib.import_module("algobot.engine.lifecycle")
        return True
    except ImportError:
        return False


def _seed() -> None:
    with session_scope() as s:
        s.add_all([
            StrategyRow(strategy_id=LIVE_SID, category="intraday", mode="live",
                        params_json={}, capital_alloc=200_000.0, enabled=True),
            StrategyRow(strategy_id=PAPER_SID, category="swing", mode="paper",
                        params_json={}, capital_alloc=100_000.0, enabled=True),
        ])
        s.add_all([
            TradeRow(strategy_id=LIVE_SID, mode="live", symbol="NSE:SBIN-EQ",
                     direction="long", qty=100,
                     entry_time=NOW - dt.timedelta(hours=2), exit_time=NOW,
                     entry_price=800.0, exit_price=810.5, gross_pnl=1050.0,
                     costs=50.0, net_pnl=1000.0, exit_reason="tp"),
            TradeRow(strategy_id=PAPER_SID, mode="paper", symbol="NSE:TCS-EQ",
                     direction="short", qty=10,
                     entry_time=NOW - dt.timedelta(hours=3),
                     exit_time=NOW - dt.timedelta(minutes=30),
                     entry_price=4100.0, exit_price=4118.0, gross_pnl=-180.0,
                     costs=20.0, net_pnl=-200.0, exit_reason="sl"),
            TradeRow(strategy_id=LIVE_SID, mode="live", symbol="NSE:INFY-EQ",
                     direction="long", qty=50,
                     entry_time=OLD - dt.timedelta(hours=1), exit_time=OLD,
                     entry_price=1500.0, exit_price=1511.0, gross_pnl=550.0,
                     costs=50.0, net_pnl=500.0, exit_reason="signal"),
        ])
        s.add_all([
            PositionRow(strategy_id=LIVE_SID, mode="live", symbol="NSE:SBIN-EQ",
                        qty=100, avg_price=805.0, stop_loss=795.0,
                        opened_at=NOW - dt.timedelta(hours=1), status="open",
                        last_price=807.0, unrealized_pnl=200.0),
            PositionRow(strategy_id=PAPER_SID, mode="paper", symbol="NSE:TCS-EQ",
                        qty=-10, avg_price=4100.0,
                        opened_at=NOW - dt.timedelta(hours=2), status="open"),
            PositionRow(strategy_id=LIVE_SID, mode="live", symbol="NSE:INFY-EQ",
                        qty=50, avg_price=1500.0, opened_at=OLD, status="closed"),
        ])
        s.add(GateStatusRow(
            strategy_id=PAPER_SID, paper_trades_count=12, oos_backtest_months=2.0,
            profit_factor=1.1, eligible=False,
            detail_json={"reason": "insufficient paper trades", "needed": 60,
                         "have": 12},
            evaluated_at=NOW))
        s.add(RiskStateRow(date=TODAY, realized_day_pnl=800.0,
                           realized_week_pnl=800.0, open_position_count=2,
                           trades_today=2, kill_switch=False))
        s.add_all([
            BacktestRunRow(strategy_id=LIVE_SID, start=TODAY - dt.timedelta(days=365),
                           end=TODAY, metrics_json={"profit_factor": 1.6}),
            BacktestRunRow(strategy_id=PAPER_SID, start=TODAY - dt.timedelta(days=365),
                           end=TODAY, metrics_json={"profit_factor": 1.2}),
        ])
        s.add_all([
            EquitySnapshotRow(ts=NOW - dt.timedelta(hours=1), strategy_id=LIVE_SID,
                              mode="live", equity=201_000.0, day_pnl=1000.0),
            EquitySnapshotRow(ts=NOW, strategy_id=PAPER_SID, mode="paper",
                              equity=99_800.0, day_pnl=-200.0),
        ])
        s.add_all([
            EventLogRow(ts=dt.datetime.utcnow(), level="info", source="engine",
                        message="scan cycle complete"),
            EventLogRow(ts=dt.datetime.utcnow() - dt.timedelta(hours=2),
                        level="error", source="broker",
                        message="order rejected", detail_json={"code": 42}),
        ])


@pytest.fixture(scope="module")
def client():
    app = create_app(start_worker=False)
    with TestClient(app) as c:   # context manager runs lifespan -> init_db()
        _seed()
        yield c


@pytest.fixture()
def worker() -> QueryWorker:
    return QueryWorker(poll_interval=0.01)


def _run_job(client, worker, body: dict) -> dict:
    """POST /queries -> assert 202 -> process_once -> return finished job."""
    r = client.post("/queries", json=body)
    assert r.status_code == 202
    ack = r.json()
    assert ack["status"] == "queued" and ack["poll"] == f"/queries/{ack['id']}"
    assert worker.process_once() is True
    r2 = client.get(ack["poll"])
    assert r2.status_code == 200
    return r2.json()


# --------------------------------------------------------------------------- meta
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db_ok"] is True
    assert body["status"] == "ok"
    assert isinstance(body["market_open"], bool)
    assert body["time_ist"].startswith(str(TODAY.year))


def test_root_catalog_for_hermes(client):
    body = client.get("/").json()
    assert body["service"] == "algobot-api"
    assert "POST /queries" in body["endpoints"]
    usage = body["usage"]
    assert "pnl" in usage["query_types"]
    assert "killswitch" in usage["query_types"]
    assert "flow" in usage["job_queue"]
    assert any(rt["type"] == "pnl" for rt in usage["freeform_routing"]["routes"])


# --------------------------------------------------------------------------- reads
def test_status_counts_and_pnl(client):
    body = client.get("/status").json()
    assert body["strategies"]["total"] == 2
    assert body["strategies"]["by_mode"] == {"live": 1, "paper": 1}
    assert body["open_positions"] == 2
    assert body["trades_today"]["count"] == 2
    assert body["pnl_today"]["by_mode"]["live"] == 1000.0
    assert body["pnl_today"]["by_mode"]["paper"] == -200.0
    assert body["pnl_today"]["total"] == 800.0
    assert body["kill_switch"] is False
    assert body["engine_alive"] is True   # engine heartbeat seeded just now


def test_strategies_list_and_detail(client):
    items = client.get("/strategies").json()
    ids = {i["strategy_id"] for i in items}
    assert {LIVE_SID, PAPER_SID} <= ids

    detail = client.get(f"/strategies/{LIVE_SID}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["in_db"] is True
    assert body["strategy"]["mode"] == "live"
    assert body["pnl_today"] == 1000.0
    assert len(body["open_positions"]) == 1
    assert len(body["recent_trades"]) == 2

    assert client.get("/strategies/no_such_strategy").status_code == 404


def test_positions_filters(client):
    assert len(client.get("/positions").json()) == 2          # open only
    live = client.get("/positions", params={"mode": "live"}).json()
    assert len(live) == 1 and live[0]["strategy_id"] == LIVE_SID
    by_sid = client.get("/positions", params={"strategy_id": PAPER_SID}).json()
    assert len(by_sid) == 1 and by_sid[0]["mode"] == "paper"
    assert len(client.get("/positions", params={"status": "all"}).json()) == 3


def test_trades_filters_order_and_limit(client):
    all_trades = client.get("/trades").json()
    assert len(all_trades) == 3
    exits = [t["exit_time"] for t in all_trades]
    assert exits == sorted(exits, reverse=True)               # newest first

    assert len(client.get("/trades", params={"mode": "paper"}).json()) == 1
    assert len(client.get("/trades",
                          params={"strategy_id": LIVE_SID}).json()) == 2

    recent = client.get("/trades", params={"from": TODAY.isoformat()}).json()
    assert len(recent) == 2
    old = client.get(
        "/trades",
        params={"to": (TODAY - dt.timedelta(days=10)).isoformat()}).json()
    assert len(old) == 1 and old[0]["net_pnl"] == 500.0

    assert len(client.get("/trades", params={"limit": 1}).json()) == 1
    assert client.get("/trades", params={"limit": 501}).status_code == 422


def test_pnl_windows_and_sparkline(client):
    body = client.get("/pnl").json()
    for window in ("today", "week", "month"):     # OLD trade is outside all three
        assert body[window]["total"] == 800.0, window
    assert body["today"]["by_mode"] == {"live": 1000.0, "paper": -200.0}
    by_strat = {(b["strategy_id"], b["mode"]): b["net_pnl"]
                for b in body["today"]["by_strategy"]}
    assert by_strat[(LIVE_SID, "live")] == 1000.0
    assert by_strat[(PAPER_SID, "paper")] == -200.0
    assert "sparkline" not in body

    spark = client.get("/pnl", params={"sparkline": "true"}).json()
    assert len(spark["sparkline"]) == 2
    assert spark["sparkline"][-1]["equity"] == 99_800.0


def test_gates(client):
    gates = client.get("/gates").json()
    row = next(g for g in gates if g["strategy_id"] == PAPER_SID)
    assert row["eligible"] is False
    assert row["detail_json"]["reason"] == "insufficient paper trades"


def test_backtests(client):
    assert len(client.get("/backtests").json()) == 2
    only = client.get("/backtests", params={"strategy_id": LIVE_SID}).json()
    assert len(only) == 1
    assert only[0]["metrics_json"]["profit_factor"] == 1.6


def test_events(client):
    all_events = client.get("/events").json()
    assert len(all_events) >= 2
    errors = client.get("/events", params={"level": "error"}).json()
    assert errors and all(e["level"] == "error" for e in errors)
    engine = client.get("/events", params={"source": "engine"}).json()
    assert engine and all(e["source"] == "engine" for e in engine)


def test_risk(client):
    body = client.get("/risk").json()
    assert body["state"]["realized_day_pnl"] == 800.0
    assert body["kill_switch"] is False
    caps = body["caps"]
    assert caps["daily_loss_cap_pct"] > 0
    assert caps["daily_loss_cap_rupees"] == pytest.approx(
        caps["capital"] * caps["daily_loss_cap_pct"] / 100)


# --------------------------------------------------------------------------- queue
def test_queue_round_trip_typed(client, worker):
    r = client.post("/queries", json={"type": "status"})
    assert r.status_code == 202
    ack = r.json()
    assert client.get(ack["poll"]).json()["status"] == "queued"

    assert worker.process_once() is True
    job = client.get(ack["poll"]).json()
    assert job["status"] == "done"
    assert job["result"]["type"] == "status"
    assert job["result"]["data"]["open_positions"] == 2
    assert job["finished_at"] is not None

    done = client.get("/queries", params={"status": "done"}).json()
    assert ack["id"] in {j["id"] for j in done}
    assert worker.process_once() is False      # queue drained


def test_queue_typed_with_params(client, worker):
    job = _run_job(client, worker,
                   {"type": "trades", "params": {"mode": "live", "limit": 5}})
    assert job["status"] == "done"
    data = job["result"]["data"]
    assert len(data) == 2 and all(t["mode"] == "live" for t in data)


def test_queue_freeform_pnl(client, worker):
    job = _run_job(client, worker, {"question": "what's my pnl today?"})
    assert job["status"] == "done"
    assert job["result"]["type"] == "pnl"
    assert job["result"]["data"]["today"]["total"] == 800.0
    assert "note" in job["result"]


def test_queue_freeform_unmapped(client, worker):
    job = _run_job(client, worker, {"question": "sing me a nice song"})
    assert job["status"] == "done"
    assert job["result"]["type"] == "unmapped"
    data = job["result"]["data"]
    assert "pnl" in data["available_types"]
    assert "hint" in data and "couldn't map" in data["answer"]


def test_queue_error_path_missing_param(client, worker):
    job = _run_job(client, worker, {"type": "strategy_detail", "params": {}})
    assert job["status"] == "error"
    assert "strategy_id" in job["result"]["error"]


def test_queue_rejects_empty_payload(client):
    assert client.post("/queries", json={}).status_code == 422


def test_query_unknown_id_404(client):
    assert client.get("/queries/does-not-exist").status_code == 404


# --------------------------------------------------------------------------- control
def test_promote_unknown_strategy_404(client):
    r = client.post("/strategies/no_such_strategy/promote",
                    json={"target_mode": "live"})
    assert r.status_code == 404   # checked against DB+registry before lazy import


def test_promote_gated_or_engine_missing(client):
    """409 when engine.lifecycle exists and the gate blocks; 503 until the
    parallel engine workstream ships the module."""
    r = client.post(f"/strategies/{PAPER_SID}/promote",
                    json={"target_mode": "live"})
    if _engine_lifecycle_importable():
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["gate"] == {"reason": "insufficient paper trades",
                                  "needed": 60, "have": 12}
    else:
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert "error" in detail and "hint" in detail


def test_demote_unknown_404_and_engine_dependency(client):
    assert client.post("/strategies/no_such_strategy/demote",
                       json={"target_mode": "off"}).status_code == 404
    r = client.post(f"/strategies/{LIVE_SID}/demote",
                    json={"target_mode": "paper"})
    if _engine_lifecycle_importable():
        assert r.status_code == 200 and r.json()["ok"] is True
    else:
        assert r.status_code == 503


def test_promote_bad_target_mode_422(client):
    r = client.post(f"/strategies/{LIVE_SID}/promote",
                    json={"target_mode": "backtest"})
    assert r.status_code == 422


def test_evaluate_gates_engine_dependency(client):
    r = client.post("/gates/evaluate", json={})
    if r.status_code == 503:
        assert "hint" in r.json()["detail"]
    else:
        assert r.status_code == 200
    assert client.post("/gates/evaluate",
                       json={"strategy_id": "no_such"}).status_code == 404


def _kill_state() -> tuple[bool, str | None]:
    with session_scope() as s:
        row = s.get(RiskStateRow, now_ist().date())
        return (bool(row.kill_switch), row.kill_reason) if row else (False, None)


def test_killswitch_rest_both_ways(client):
    r = client.post("/killswitch", json={"on": True, "reason": "api test"})
    assert r.status_code == 200 and r.json()["kill_switch"] is True
    on, reason = _kill_state()
    assert on is True and reason == "api test"
    assert client.get("/risk").json()["kill_switch"] is True
    assert client.get("/status").json()["kill_switch"] is True

    r = client.post("/killswitch", json={"on": False, "reason": "all clear"})
    assert r.status_code == 200 and r.json()["kill_switch"] is False
    assert _kill_state()[0] is False
    assert client.get("/risk").json()["kill_switch"] is False


def test_killswitch_via_queue(client, worker):
    job = _run_job(client, worker,
                   {"type": "killswitch", "params": {"on": True,
                                                     "reason": "queue test"}})
    assert job["status"] == "done"
    assert job["result"]["data"]["kill_switch"] is True
    assert _kill_state() == (True, "queue test")

    job = _run_job(client, worker,
                   {"type": "killswitch", "params": {"on": False}})
    assert job["status"] == "done"
    assert _kill_state()[0] is False
