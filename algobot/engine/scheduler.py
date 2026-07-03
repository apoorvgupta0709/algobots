"""Engine service entrypoint: ``python -m algobot.engine.scheduler``.

Boots the trading stack (feed, brokers, risk, order manager, runner), then
drives all scheduled jobs on an APScheduler BlockingScheduler pinned to
Asia/Kolkata. Every job is guarded by the market clock and hardened so a
single failure never kills the process; liveness is journalled as a 60-second
heartbeat that the API's /status endpoint reads.

Run one job and exit with ``--once <job>`` (bypasses trading-day guards):
scan_5min | scan_15min | scan_0920 | eod | monitor | gate | squareoff | snapshot
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import signal
import sys
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from algobot.core import clock, registry
from algobot.core.config import settings
from algobot.core.enums import Mode
from algobot.core.exceptions import AuthError
from algobot.core.strategy import (
    SCAN_0920_ONCE,
    SCAN_EOD,
    SCAN_EVERY_5MIN,
    SCAN_EVERY_15MIN,
    SCAN_EXPIRY_DAY,
    SCAN_MONTHLY,
    SCAN_WEEKLY,
)
from algobot.data.cache import CachedFeed
from algobot.engine import gate, lifecycle
from algobot.engine.runner import StrategyRunner
from algobot.execution.order_manager import OrderManager
from algobot.execution.risk import RiskEngine
from algobot.execution.squareoff import squareoff_intraday
from algobot.persistence.db import init_db, session_scope
from algobot.persistence.schema import EventLogRow

log = logging.getLogger(__name__)

TIMEZONE = "Asia/Kolkata"
LAST_INTRADAY_SCAN = dt.time(15, 10)   # 5/15-min scans run 09:20-15:10 IST
HEARTBEAT_SEC = 60
GATE_EVAL_TIME = dt.time(16, 30)
RISK_ROLLOVER_TIME = dt.time(9, 0)

ONCE_JOBS = ("scan_5min", "scan_15min", "scan_0920", "eod",
             "monitor", "gate", "squareoff", "snapshot")


# --------------------------------------------------------------------------- helpers
def _journal(level: str, message: str, detail: dict | None = None) -> None:
    """Write to event_log, source='engine'. Never raises."""
    try:
        with session_scope() as s:
            s.add(EventLogRow(level=level, source="engine", message=message,
                              detail_json=detail))
    except Exception:
        log.exception("failed to journal: %s", message)


def _parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = str(value).split(":")
    return int(hh), int(mm)


def _is_last_trading_day_of_week(day: dt.date) -> bool:
    return clock.week_start(clock.next_trading_day(day)) != clock.week_start(day)


def _is_first_trading_day_of_month(day: dt.date) -> bool:
    return clock.prev_trading_day(day).month != day.month


def _in_scan_window(ts: dt.datetime) -> bool:
    return clock.FIRST_SCAN <= ts.time() <= LAST_INTRADAY_SCAN


# --------------------------------------------------------------------------- engine
class EngineService:
    """Owns the whole trading stack and exposes one method per scheduled job."""

    def __init__(self) -> None:
        self.live_enabled = False
        self.feed = self._build_feed()
        self.risk = RiskEngine()
        brokers = self._build_brokers()
        self.order_manager = OrderManager(brokers, self.risk)
        self.runner = StrategyRunner(self.feed, self.order_manager, self.risk)

    # -------------------------------------------------------------- stack build
    def _build_feed(self) -> CachedFeed:
        """Authenticated Fyers feed when possible, else cache-only paper feed."""
        try:
            from algobot.broker.fyers.auth import get_fyers_client
            from algobot.data.fyers_feed import FyersFeed

            client = get_fyers_client()
            self._fyers_client = client
            self.live_enabled = True
            log.info("Fyers authenticated: live data + live routing available")
            return CachedFeed(FyersFeed(client))
        except AuthError as exc:
            reason = str(exc)
        except Exception as exc:  # missing package, network, ...
            reason = f"{type(exc).__name__}: {exc}"
        self._fyers_client = None
        self.live_enabled = False
        msg = ("Fyers unavailable — engine running PAPER-ONLY on cached data "
               f"({reason}). Live mode strategies will NOT trade.")
        log.warning(msg)
        _journal("warn", msg)
        return CachedFeed(None)

    def _build_brokers(self) -> dict:
        from algobot.broker.paper import PaperBroker

        brokers = {Mode.PAPER: PaperBroker(quote_fn=self._safe_quotes)}
        if self.live_enabled:
            from algobot.broker.fyers.broker import FyersBroker
            brokers[Mode.LIVE] = FyersBroker(client=self._fyers_client)
        return brokers

    def _safe_quotes(self, symbols: list[str]) -> dict[str, float]:
        try:
            return dict(self.feed.get_quotes(list(symbols)) or {})
        except Exception:
            log.debug("quote fetch failed", exc_info=True)
            return {}

    # -------------------------------------------------------------- jobs
    def job_token_refresh(self) -> None:
        if not clock.is_trading_day():
            return
        try:
            from algobot.broker.fyers.auth import get_access_token
            get_access_token()
            log.info("Fyers token refreshed")
            _journal("info", "Fyers token refreshed")
        except Exception as exc:
            log.exception("TOKEN REFRESH FAILED")
            _journal("error",
                     f"TOKEN REFRESH FAILED — live trading is at risk: {exc}")

    def job_scan_5min(self) -> None:
        now = clock.now_ist()
        if not clock.is_trading_day(now.date()) or not _in_scan_window(now):
            return
        self.runner.scan(SCAN_EVERY_5MIN, now=now)

    def job_scan_15min(self) -> None:
        now = clock.now_ist()
        if not clock.is_trading_day(now.date()) or not _in_scan_window(now):
            return
        self.runner.scan(SCAN_EVERY_15MIN, now=now)

    def job_scan_expiry(self) -> None:
        now = clock.now_ist()
        if not clock.is_trading_day(now.date()) or not _in_scan_window(now):
            return
        try:
            from algobot.data.expiries import is_expiry_day
            if not is_expiry_day("NIFTY", now.date()):
                return
        except Exception:
            log.exception("expiry-day check failed; skipping expiry scan")
            return
        self.runner.scan(SCAN_EXPIRY_DAY, now=now)

    def job_scan_0920(self) -> None:
        if not clock.is_trading_day():
            return
        self.runner.scan(SCAN_0920_ONCE)

    def job_monitor(self) -> None:
        if not clock.is_market_open():
            return
        self.runner.monitor_tick()

    def job_squareoff(self) -> None:
        if not clock.is_trading_day():
            return
        try:
            metas = {sid: cls.meta
                     for sid, cls in registry.all_strategies().items()}
            orders = squareoff_intraday(self.order_manager, metas)
            if orders:
                _journal("info", f"squareoff flattened {len(orders)} positions")
        except Exception as exc:
            log.exception("squareoff job failed")
            _journal("error", f"squareoff job failed: {exc}")

    def job_eod(self) -> None:
        if not clock.is_trading_day():
            return
        now = clock.now_ist()
        self.runner.scan(SCAN_EOD, now=now)
        if _is_last_trading_day_of_week(now.date()):
            self.runner.scan(SCAN_WEEKLY, now=now)
        if _is_first_trading_day_of_month(now.date()):
            self.runner.scan(SCAN_MONTHLY, now=now)

    def job_gate(self) -> None:
        if not clock.is_trading_day():
            return
        results = gate.evaluate_all()
        eligible = sorted(sid for sid, ok in results.items() if ok)
        log.info("gate evaluated %d strategies; eligible: %s",
                 len(results), eligible or "none")

    def job_snapshot(self) -> None:
        if not clock.is_market_open():
            return
        self.runner.snapshot_equity()

    def job_risk_rollover(self) -> None:
        if not clock.is_trading_day():
            return
        try:
            state = self.risk.day_state()   # creates today's row, refreshes week P&L
            log.info("risk day-state rolled over: week_pnl=%.2f",
                     state.realized_week_pnl)
        except Exception as exc:
            log.exception("risk rollover failed")
            _journal("error", f"risk day-state rollover failed: {exc}")

    @staticmethod
    def job_heartbeat() -> None:
        _journal("info", "heartbeat")

    # -------------------------------------------------------------- once map
    def run_once(self, job: str) -> None:
        """Run one job immediately (guards bypassed) and return."""
        now = clock.now_ist()
        actions = {
            "scan_5min": lambda: self.runner.scan(SCAN_EVERY_5MIN, now=now),
            "scan_15min": lambda: self.runner.scan(SCAN_EVERY_15MIN, now=now),
            "scan_0920": lambda: self.runner.scan(SCAN_0920_ONCE, now=now),
            "eod": self.job_eod,
            "monitor": self.runner.monitor_tick,
            "gate": self.job_gate,
            "squareoff": self.job_squareoff,
            "snapshot": self.runner.snapshot_equity,
        }
        log.info("running one-shot job: %s", job)
        actions[job]()


# --------------------------------------------------------------------------- wiring
def build_scheduler(engine: EngineService) -> BlockingScheduler:
    """All jobs on one BlockingScheduler; in-job guards handle the market clock."""
    eng_cfg = settings()["engine"]
    sched = BlockingScheduler(timezone=TIMEZONE)

    th, tm = _parse_hhmm(eng_cfg["token_refresh_time"])
    sched.add_job(engine.job_token_refresh, CronTrigger(hour=th, minute=tm),
                  id="token_refresh", name="daily Fyers token refresh")

    sched.add_job(engine.job_scan_5min,
                  CronTrigger(minute="*/5", hour="9-15"),
                  id="scan_5min", name="5-min scan (09:20-15:10)")
    sched.add_job(engine.job_scan_15min,
                  CronTrigger(minute="*/15", hour="9-15"),
                  id="scan_15min", name="15-min scan (09:20-15:10)")
    sched.add_job(engine.job_scan_expiry,
                  CronTrigger(minute="*/5", hour="9-15"),
                  id="scan_expiry", name="expiry-day 5-min scan")
    sched.add_job(engine.job_scan_0920, CronTrigger(hour=9, minute=20),
                  id="scan_0920", name="09:20 one-shot scan")

    sched.add_job(engine.job_monitor,
                  IntervalTrigger(seconds=int(eng_cfg["monitor_interval_sec"])),
                  id="monitor", name="position monitor",
                  max_instances=1, coalesce=True)

    qh, qm = _parse_hhmm(eng_cfg["squareoff_time"])
    sched.add_job(engine.job_squareoff, CronTrigger(hour=qh, minute=qm),
                  id="squareoff", name="intraday squareoff")

    eh, em = _parse_hhmm(eng_cfg["eod_scan_time"])
    sched.add_job(engine.job_eod, CronTrigger(hour=eh, minute=em),
                  id="eod", name="EOD scan (+weekly/monthly)")

    sched.add_job(engine.job_gate,
                  CronTrigger(hour=GATE_EVAL_TIME.hour,
                              minute=GATE_EVAL_TIME.minute),
                  id="gate", name="paper-to-live gate evaluation")

    sched.add_job(engine.job_snapshot, CronTrigger(minute="*/5"),
                  id="snapshot", name="equity snapshots (market hours)")

    sched.add_job(engine.job_risk_rollover,
                  CronTrigger(hour=RISK_ROLLOVER_TIME.hour,
                              minute=RISK_ROLLOVER_TIME.minute),
                  id="risk_rollover", name="risk day-state rollover")

    sched.add_job(engine.job_heartbeat,
                  IntervalTrigger(seconds=HEARTBEAT_SEC),
                  id="heartbeat", name="liveness heartbeat",
                  max_instances=1, coalesce=True)
    return sched


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m algobot.engine.scheduler",
        description="Algobot engine service (scans, monitor, squareoff, gate)")
    parser.add_argument("--once", choices=ONCE_JOBS, metavar="JOB",
                        help=f"run one job and exit ({'|'.join(ONCE_JOBS)})")
    args = parser.parse_args(argv)

    init_db()
    lifecycle.sync_config_to_db()
    engine = EngineService()

    if args.once:
        engine.run_once(args.once)
        return 0

    sched = build_scheduler(engine)

    def _sigterm(signum, frame):  # pragma: no cover - process teardown
        log.info("SIGTERM received: shutting down scheduler")
        _journal("info", "engine stopping (SIGTERM)")
        sched.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _sigterm)

    _journal("info", "engine started",
             {"live_enabled": engine.live_enabled,
              "jobs": [j.id for j in sched.get_jobs()]})
    log.info("engine started (live_enabled=%s); scheduler running",
             engine.live_enabled)
    try:
        sched.start()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        log.info("KeyboardInterrupt: shutting down")
    finally:
        _journal("info", "engine stopped")
        log.info("engine stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
