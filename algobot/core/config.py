"""Settings loader: config/*.yaml merged with environment variables.

Precedence: env var > yaml > default. DB-level per-strategy overrides
(strategies table) are applied later by engine/lifecycle.py.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

load_dotenv(REPO_ROOT / ".env")


def _read_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


@lru_cache(maxsize=None)
def settings() -> dict[str, Any]:
    cfg = _read_yaml("settings.yaml")
    cfg.setdefault("capital", 500_000)
    cfg.setdefault("risk", {})
    cfg["risk"].setdefault("risk_per_trade_pct", 0.75)     # % of capital risked per trade
    cfg["risk"].setdefault("daily_loss_cap_pct", 2.5)
    cfg["risk"].setdefault("weekly_loss_cap_pct", 5.0)
    cfg["risk"].setdefault("max_concurrent_positions", 3)
    cfg["risk"].setdefault("max_trades_per_day", 10)       # global, across strategies
    cfg["risk"].setdefault("breakeven_at_r", 0.8)
    cfg["risk"].setdefault("ratchet_lock_pct", 60)
    cfg.setdefault("engine", {})
    cfg["engine"].setdefault("scan_interval_min", 5)
    cfg["engine"].setdefault("monitor_interval_sec", 15)
    cfg["engine"].setdefault("squareoff_time", "15:15")
    cfg["engine"].setdefault("eod_scan_time", "15:45")
    cfg["engine"].setdefault("token_refresh_time", "08:45")
    cfg["engine"].setdefault("max_candle_staleness_min", 20)  # drop intraday data older than this in live scans
    cfg.setdefault("data_cache_dir", str(REPO_ROOT / "data" / "cache"))
    cfg["database_url"] = os.getenv(
        "DATABASE_URL", cfg.get("database_url", f"sqlite:///{REPO_ROOT}/data/algobot.db"))
    return cfg


_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", ""})


def live_orders_enabled() -> bool:
    """Hard paper-only fuse. Live order routing is allowed ONLY when this
    returns True; it defaults to False and fails closed on any malformed
    value (SystemExit, matching the legacy scripts' loader convention).

    Precedence: env ALGOBOT_LIVE_ORDERS_ENABLED > settings.yaml
    live_orders_enabled > False. Deliberately not cached: read at boot and
    at every mode change / order placement.
    """
    raw: Any = os.getenv("ALGOBOT_LIVE_ORDERS_ENABLED")
    source = "env ALGOBOT_LIVE_ORDERS_ENABLED"
    if raw is None:
        raw = _read_yaml("settings.yaml").get("live_orders_enabled", False)
        source = "settings.yaml live_orders_enabled"
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    raise SystemExit(
        f"Unsafe config: {source}={raw!r} is not a strict boolean; "
        "refusing to start (fail-closed live-orders fuse)")


@lru_cache(maxsize=None)
def gate_config() -> dict[str, Any]:
    cfg = _read_yaml("gate.yaml")
    cfg.setdefault("min_paper_trades", 60)
    cfg.setdefault("min_oos_backtest_months", 6)
    cfg.setdefault("min_profit_factor", 1.3)
    cfg.setdefault("max_drawdown_pct", 15.0)
    cfg.setdefault("stop_fire_tolerance_pct", 0.5)   # avg |fill - modeled| / modeled
    cfg.setdefault("synthetic_backtest_discount", 0.5)  # weight for synthetic-data runs
    return cfg


def strategies_config() -> dict[str, Any]:
    """Per-strategy config from config/strategies.yaml: {strategy_id: {mode, params, capital}}."""
    return _read_yaml("strategies.yaml").get("strategies", {}) or {}


def strategies_defaults() -> dict[str, Any]:
    return _read_yaml("strategies.yaml").get("defaults", {}) or {}


def env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)
