"""Hard paper-only fuse tests.

The fuse (``algobot.core.config.live_orders_enabled``) must default CLOSED,
fail closed on malformed values, and be enforced at every layer: scheduler
broker wiring, lifecycle promotion, and FyersBroker.place_order itself.

No network. A throwaway sqlite DATABASE_URL is set BEFORE any algobot import
(same convention as tests/test_engine.py).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="algobot-fuse-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"

import pytest

from algobot.core import config as config_mod

config_mod.settings.cache_clear()
config_mod.gate_config.cache_clear()

from algobot.persistence import db as db_mod

db_mod.get_engine.cache_clear()
db_mod.get_sessionmaker.cache_clear()

from algobot.broker.fyers.broker import FyersBroker
from algobot.core.enums import Mode, OrderType, ProductType, Side
from algobot.core.exceptions import BrokerError
from algobot.core.models import Order
from algobot.persistence.db import init_db

REPO_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ config fuse
class TestFuseConfig:
    def test_defaults_closed(self, monkeypatch):
        monkeypatch.delenv("ALGOBOT_LIVE_ORDERS_ENABLED", raising=False)
        assert config_mod.live_orders_enabled() is False

    def test_env_opens_fuse(self, monkeypatch):
        monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", "true")
        assert config_mod.live_orders_enabled() is True

    def test_env_false_strings(self, monkeypatch):
        for value in ("false", "0", "no", "off", ""):
            monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", value)
            assert config_mod.live_orders_enabled() is False

    def test_malformed_value_fails_closed_with_systemexit(self, monkeypatch):
        monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", "definitely")
        with pytest.raises(SystemExit):
            config_mod.live_orders_enabled()

    def test_shipped_settings_yaml_keeps_fuse_closed(self, monkeypatch):
        monkeypatch.delenv("ALGOBOT_LIVE_ORDERS_ENABLED", raising=False)
        shipped = config_mod._read_yaml("settings.yaml")
        assert shipped.get("live_orders_enabled") is False


# ------------------------------------------------------------------ broker layer
def _dummy_order() -> Order:
    return Order(strategy_id="zz98_dummy", mode=Mode.LIVE,
                 symbol="NSE:TESTSYM-EQ", side=Side.BUY, qty=1,
                 order_type=OrderType.MARKET, product_type=ProductType.INTRADAY)


class _ExplodingClient:
    """A fake fyers client that fails the test if any order API is touched."""

    def place_order(self, data):  # pragma: no cover - must never be reached
        raise AssertionError("place_order reached the fyers client with fuse closed")


class TestBrokerLayer:
    def test_place_order_refused_while_fuse_closed(self, monkeypatch):
        monkeypatch.delenv("ALGOBOT_LIVE_ORDERS_ENABLED", raising=False)
        broker = FyersBroker(client=_ExplodingClient())
        order = _dummy_order()
        with pytest.raises(BrokerError, match="fuse"):
            broker.place_order(order)
        assert order.status.name == "REJECTED"

    def test_place_order_passes_through_when_fuse_open(self, monkeypatch):
        monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", "true")

        class OkClient:
            def place_order(self, data):
                return {"s": "ok", "id": "FY123"}

        broker = FyersBroker(client=OkClient())
        placed = broker.place_order(_dummy_order())
        assert placed.broker_order_id == "FY123"


# ------------------------------------------------------------------ scheduler wiring
class TestSchedulerWiring:
    @pytest.fixture()
    def fake_auth(self, monkeypatch):
        """Make Fyers auth 'succeed' without any network."""
        init_db()
        from algobot.broker.fyers import auth as auth_mod

        class FakeClient:
            def quotes(self, data):
                return {"s": "ok", "d": []}

        monkeypatch.setattr(auth_mod, "get_fyers_client", lambda: FakeClient())

    def test_no_live_broker_when_fuse_closed(self, fake_auth, monkeypatch):
        monkeypatch.delenv("ALGOBOT_LIVE_ORDERS_ENABLED", raising=False)
        from algobot.engine.scheduler import EngineService

        engine = EngineService()
        assert engine._fyers_auth_ok is True
        assert engine.live_enabled is False
        assert Mode.LIVE not in engine.order_manager.brokers

    def test_live_broker_wired_when_fuse_open(self, fake_auth, monkeypatch):
        monkeypatch.setenv("ALGOBOT_LIVE_ORDERS_ENABLED", "true")
        from algobot.engine.scheduler import EngineService

        engine = EngineService()
        assert engine.live_enabled is True
        assert Mode.LIVE in engine.order_manager.brokers


# ------------------------------------------------------------------ legacy s102
def test_legacy_s102_refuses_without_ack():
    env = {k: v for k, v in os.environ.items() if k != "ALGOBOT_LEGACY_LIVE_ACK"}
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "codesfiles" / "s102_algobotstart.py")],
        capture_output=True, text=True, timeout=30, env=env, check=False,
    )
    assert result.returncode != 0
    assert "Refusing to run" in (result.stderr + result.stdout)
