#!/usr/bin/env python3
"""Pre-market FYERS / data readiness watchdog for the BankNifty paper engine.

Read-only. Verifies — BEFORE the entry window opens — that the paper engine will
actually be able to take entries, so a silent FYERS-auth or quote-ingestion
outage is surfaced loudly instead of quietly blocking trades all morning.

It checks:
  * config safety — the campaign config is still paper-safe
    (``paper_only`` true, ``live_orders_enabled`` false);
  * FYERS credentials — required env vars are PRESENT (presence only — this
    script never reads, logs, or prints any credential value);
  * quote-ingest evidence — the most recent FYERS quote ingestion run
    (``market.ingestion_runs``) succeeded; a newer failed run (e.g. an
    auth/token failure) blocks readiness even if an older success is still
    within the freshness window, and a successful run also ran recently;
  * quote coverage/freshness — the configured underlying and a configurable
    fraction of configured constituents have fresh quotes (``market.quotes``)
    that are actually usable for entries: a non-null ``ltp`` and a positive
    ``open`` (the engine needs ``open`` to compute ``pct_from_open`` before
    entries), using the same ``updated_at`` staleness clock the engine uses.

Safety: this is research/paper tooling. It NEVER places orders, NEVER calls
FYERS, opens its PostgreSQL connection read-only, and reports credentials by
presence/absence only. It does NOT create or modify cron jobs — scheduling is a
manual operator step (see scripts/premarket_readiness_watchdog.sh).

Exit codes:
  0  ready — or only data checks failing but still before the alert deadline
     (there is still time for ingestion to come up before entries begin)
  1  data readiness failed at/after the alert deadline (entry imminent), or
     --strict was passed
  2  a required check failed (config unsafe or credentials missing) — always hard

Designed to run a few times in the pre-entry window (~09:10–09:35 IST), e.g. via
cron at 09:15, 09:25 and 09:32 so a late ingestion start is caught before 09:35.

Examples:
  uv run python scripts/premarket_readiness_watchdog.py
  uv run python scripts/premarket_readiness_watchdog.py \
      --as-of 2026-06-16T09:32:00+05:30 --strict
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg
from dotenv import load_dotenv

import scripts.banknifty_options_paper as bn

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_CONFIG = Path("config/banknifty_options_paper.json")
DEFAULT_DB_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"

# FYERS quote ingestion only needs the client id + access token. The secret key
# / redirect uri are needed to re-mint a token, so they are advisory: missing
# them does not block today but is worth flagging at the daily token refresh.
REQUIRED_CREDS = ("FYERS_CLIENT_ID", "FYERS_ACCESS_TOKEN")
ADVISORY_CREDS = ("FYERS_SECRET_KEY", "FYERS_REDIRECT_URI")

REQUIRED = "required"
DATA = "data"

PASS = "pass"
FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS | FAIL
    severity: str  # REQUIRED | DATA
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == PASS


@dataclass
class ReadinessReport:
    as_of: datetime
    alert_deadline: dtime
    checks: list[CheckResult]
    strict: bool = False

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    @property
    def ready(self) -> bool:
        return not self.failures

    @property
    def required_failed(self) -> bool:
        return any(c.severity == REQUIRED and not c.ok for c in self.checks)

    @property
    def data_failed(self) -> bool:
        return any(c.severity == DATA and not c.ok for c in self.checks)

    @property
    def past_deadline(self) -> bool:
        as_of_ist = self.as_of.astimezone(IST)
        deadline = as_of_ist.replace(
            hour=self.alert_deadline.hour,
            minute=self.alert_deadline.minute,
            second=0,
            microsecond=0,
        )
        return as_of_ist >= deadline

    @property
    def exit_code(self) -> int:
        if self.required_failed:
            return 2
        if self.data_failed and (self.strict or self.past_deadline):
            return 1
        return 0


# --------------------------------------------------------------------------- #
# Pure check functions — fed already-fetched data so they are trivially tested
# --------------------------------------------------------------------------- #
def _age_seconds(as_of: datetime, ts: datetime | None) -> float | None:
    if ts is None:
        return None
    return (as_of - ts).total_seconds()


def _fmt_ist(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    return ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _open_is_usable(row: dict | None) -> bool:
    """Mirror the engine's ``pct_from_open`` open-price semantics: a quote is
    only usable for entries when ``open`` is present and strictly positive."""
    if not row:
        return False
    open_value = row.get("open")
    if open_value is None:
        return False
    try:
        return Decimal(str(open_value)) > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def check_config_safety(raw: dict) -> CheckResult:
    try:
        paper_only = bn.strict_bool_from_config(raw.get("paper_only", True), key="paper_only")
        live = bn.strict_bool_from_config(raw.get("live_orders_enabled", False), key="live_orders_enabled")
    except SystemExit as exc:
        return CheckResult("config_safety", FAIL, REQUIRED, str(exc))
    if not paper_only:
        return CheckResult("config_safety", FAIL, REQUIRED, "paper_only is not true")
    if live:
        return CheckResult("config_safety", FAIL, REQUIRED, "live_orders_enabled is true")
    return CheckResult("config_safety", PASS, REQUIRED, "paper_only=true, live_orders_enabled=false")


def check_credentials(present: dict[str, bool]) -> CheckResult:
    """Report credential presence ONLY — never the values."""
    missing_required = [k for k in REQUIRED_CREDS if not present.get(k)]
    missing_advisory = [k for k in ADVISORY_CREDS if not present.get(k)]
    present_keys = [k for k in REQUIRED_CREDS + ADVISORY_CREDS if present.get(k)]
    detail_parts = [f"present: {', '.join(present_keys) or 'none'}"]
    if missing_required:
        detail_parts.append(f"MISSING required: {', '.join(missing_required)}")
    if missing_advisory:
        detail_parts.append(f"missing advisory: {', '.join(missing_advisory)}")
    status = FAIL if missing_required else PASS
    return CheckResult("credentials", status, REQUIRED, "; ".join(detail_parts))


def check_ingest_freshness(
    latest_run: dict | None,
    latest_success: dict | None,
    as_of: datetime,
    max_age_seconds: int,
) -> CheckResult:
    """Block readiness unless the *most recent* quotes run succeeded and is fresh.

    Looking only at the latest successful run would let a newer failed run — e.g.
    an auth/token failure — stay invisible while an older success is still inside
    the freshness window, so we evaluate the latest run regardless of status first.
    """
    if not latest_run:
        return CheckResult(
            "quote_ingest", FAIL, DATA,
            "no FYERS quote ingestion run found in market.ingestion_runs",
        )
    status = str(latest_run.get("status") or "unknown")
    if status != "success":
        # A newer non-success run (failed/error/running) means ingestion is not
        # currently healthy — surface it and the last good run for context. Only
        # status + timestamps are reported; no credential/error-body values.
        run_ts = latest_run.get("finished_at") or latest_run.get("started_at")
        last_ok_ts = (latest_success.get("finished_at") or latest_success.get("started_at")) if latest_success else None
        return CheckResult(
            "quote_ingest", FAIL, DATA,
            f"latest quote ingestion {_fmt_ist(run_ts)} status={status} (not success); "
            f"last success {_fmt_ist(last_ok_ts)}",
        )
    ts = latest_run.get("finished_at") or latest_run.get("started_at")
    age = _age_seconds(as_of, ts)
    if age is None:
        return CheckResult("quote_ingest", FAIL, DATA, "latest successful quote ingestion has no timestamp")
    if age > max_age_seconds:
        return CheckResult(
            "quote_ingest", FAIL, DATA,
            f"last successful quote ingestion {_fmt_ist(ts)} ({int(age)}s ago) older than {max_age_seconds}s",
        )
    return CheckResult(
        "quote_ingest", PASS, DATA,
        f"last successful quote ingestion {_fmt_ist(ts)} ({int(age)}s ago)",
    )


def check_underlying_quote(symbol: str, row: dict | None, as_of: datetime, max_age_seconds: int) -> CheckResult:
    if not row:
        return CheckResult("underlying_quote", FAIL, DATA, f"no quote row for underlying {symbol}")
    if row.get("ltp") is None:
        return CheckResult("underlying_quote", FAIL, DATA, f"underlying {symbol} has null ltp")
    if not _open_is_usable(row):
        return CheckResult(
            "underlying_quote", FAIL, DATA,
            f"underlying {symbol} has no positive open (pct_from_open uncomputable, entries blocked)",
        )
    age = _age_seconds(as_of, row.get("updated_at"))
    if age is None:
        return CheckResult("underlying_quote", FAIL, DATA, f"underlying {symbol} quote has no updated_at")
    if age > max_age_seconds:
        return CheckResult(
            "underlying_quote", FAIL, DATA,
            f"underlying {symbol} quote stale: {int(age)}s old (> {max_age_seconds}s)",
        )
    return CheckResult("underlying_quote", PASS, DATA, f"underlying {symbol} fresh ({int(age)}s old)")


def _quote_is_fresh(row: dict | None, as_of: datetime, max_age_seconds: int) -> bool:
    if not row or row.get("ltp") is None or not _open_is_usable(row):
        return False
    age = _age_seconds(as_of, row.get("updated_at"))
    return age is not None and age <= max_age_seconds


def check_constituent_coverage(
    constituents,
    rows: dict[str, dict],
    as_of: datetime,
    max_age_seconds: int,
    min_coverage_pct: float,
) -> CheckResult:
    total = len(constituents)
    if total == 0:
        return CheckResult("constituent_coverage", FAIL, DATA, "no constituents configured")
    fresh, stale = [], []
    for c in constituents:
        if _quote_is_fresh(rows.get(c.fyers_symbol), as_of, max_age_seconds):
            fresh.append(c.symbol)
        else:
            stale.append(c.symbol)
    coverage = len(fresh) / total * 100.0
    base = f"{len(fresh)}/{total} fresh ({coverage:.0f}% >= {min_coverage_pct:.0f}% required)"
    if coverage < min_coverage_pct:
        return CheckResult(
            "constituent_coverage", FAIL, DATA,
            f"{base}; stale/missing: {', '.join(stale)}",
        )
    detail = base if not stale else f"{base}; stale/missing: {', '.join(stale)}"
    return CheckResult("constituent_coverage", PASS, DATA, detail)


# --------------------------------------------------------------------------- #
# Read-only DB access
# --------------------------------------------------------------------------- #
def connect_readonly() -> psycopg.Connection:
    dsn = os.getenv("WATCHDOG_DATABASE_URL") or os.getenv("DATABASE_URL") or DEFAULT_DB_URL
    conn = psycopg.connect(dsn)
    # Set read-only before any query so this watchdog can never mutate data.
    conn.read_only = True
    return conn


def fetch_latest_quote_ingestion(conn: psycopg.Connection) -> tuple[dict | None, dict | None]:
    """Return ``(latest_run, latest_success)`` for ``job_type='quotes'``.

    ``latest_run`` is the most recent run regardless of status (so a fresh
    failure is visible); ``latest_success`` is the most recent successful run,
    used only to report freshness/context.
    """
    def _row(r) -> dict | None:
        if not r:
            return None
        return {"status": r[0], "started_at": r[1], "finished_at": r[2], "source": r[3]}

    with conn.cursor() as cur:
        cur.execute(
            """
            select status, started_at, finished_at, source
            from market.ingestion_runs
            where job_type = 'quotes'
            order by coalesce(finished_at, started_at) desc
            limit 1
            """
        )
        latest = cur.fetchone()
        cur.execute(
            """
            select status, started_at, finished_at, source
            from market.ingestion_runs
            where job_type = 'quotes' and status = 'success'
            order by coalesce(finished_at, started_at) desc
            limit 1
            """
        )
        success = cur.fetchone()
    return _row(latest), _row(success)


def fetch_quotes(conn: psycopg.Connection, symbols: list[str]) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "select symbol, updated_at, quote_time, ltp, open from market.quotes where symbol = any(%s)",
            (symbols,),
        )
        return {
            r[0]: {"updated_at": r[1], "quote_time": r[2], "ltp": r[3], "open": r[4]}
            for r in cur.fetchall()
        }


def credential_presence() -> dict[str, bool]:
    return {k: bool(os.getenv(k)) for k in REQUIRED_CREDS + ADVISORY_CREDS}


# --------------------------------------------------------------------------- #
# Orchestration + reporting
# --------------------------------------------------------------------------- #
def build_report(
    raw: dict,
    *,
    as_of: datetime,
    alert_deadline: dtime,
    max_quote_age_seconds: int,
    max_ingest_age_seconds: int,
    min_coverage_pct: float,
    strict: bool,
    creds: dict[str, bool] | None = None,
    latest_run: dict | None = None,
    latest_success: dict | None = None,
    quote_rows: dict[str, dict] | None = None,
    db_error: str | None = None,
) -> ReadinessReport:
    """Assemble all checks. DB-derived inputs are passed in so this stays pure."""
    underlying = str(raw.get("underlying_symbol", "NSE:NIFTYBANK-INDEX"))
    constituents = bn.parse_constituents(raw.get("constituents"))
    creds = creds if creds is not None else credential_presence()
    quote_rows = quote_rows or {}

    checks = [check_config_safety(raw), check_credentials(creds)]

    if db_error is not None:
        checks.append(CheckResult("database", FAIL, DATA, f"could not verify quote data: {db_error}"))
    else:
        checks.append(check_ingest_freshness(latest_run, latest_success, as_of, max_ingest_age_seconds))
        checks.append(check_underlying_quote(underlying, quote_rows.get(underlying), as_of, max_quote_age_seconds))
        checks.append(
            check_constituent_coverage(constituents, quote_rows, as_of, max_quote_age_seconds, min_coverage_pct)
        )

    return ReadinessReport(as_of=as_of, alert_deadline=alert_deadline, checks=checks, strict=strict)


def render_report(
    report: ReadinessReport,
    *,
    max_quote_age_seconds: int,
    max_ingest_age_seconds: int,
    min_coverage_pct: float,
) -> str:
    lines = ["=== BankNifty pre-market readiness watchdog ==="]
    lines.append(f"as_of (IST):    {report.as_of.astimezone(IST).strftime('%Y-%m-%d %H:%M:%S')}")
    deadline = report.alert_deadline.strftime("%H:%M")
    lines.append(f"entry window:   >= {deadline} IST (alert deadline; past={report.past_deadline})")
    lines.append(
        f"thresholds:     quote<={max_quote_age_seconds}s ingest<={max_ingest_age_seconds}s "
        f"coverage>={min_coverage_pct:.0f}% strict={report.strict}"
    )
    lines.append("")
    for c in report.checks:
        tag = "PASS" if c.ok else ("FAIL/req" if c.severity == REQUIRED else "FAIL")
        lines.append(f"[{tag:8}] {c.name:22} {c.detail}")
    lines.append("")
    if report.ready:
        lines.append(f"RESULT: READY -- exit {report.exit_code}")
    else:
        n = len(report.failures)
        lines.append(f"RESULT: NOT READY ({n} check{'s' if n != 1 else ''} failing) -- exit {report.exit_code}")
        lines.append(f"*** ALERT: BankNifty entry readiness FAILED before/at the entry window: "
                     f"{', '.join(c.name for c in report.failures)} ***")
    return "\n".join(lines)


def parse_as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(IST)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-market BankNifty FYERS/data readiness watchdog (read-only).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", help="ISO timestamp to evaluate against (tz-naive assumed IST). Defaults to now.")
    parser.add_argument("--alert-deadline", help="HH:MM IST entry-window deadline. Defaults to config no_entry_before.")
    parser.add_argument("--max-quote-age-seconds", type=int, help="Max quote staleness. Defaults to config quote_stale_seconds.")
    parser.add_argument("--max-ingest-age-seconds", type=int, default=600, help="Max age of last successful quote ingestion run.")
    parser.add_argument("--min-coverage-pct", type=float, help="Min %% of constituents with fresh quotes. Defaults to config min_constituent_coverage_pct.")
    parser.add_argument("--strict", action="store_true", help="Treat data-readiness failures as hard (exit 1) even before the deadline.")
    args = parser.parse_args()

    raw = json.loads(args.config.read_text(encoding="utf-8"))
    as_of = parse_as_of(args.as_of)

    deadline_str = args.alert_deadline or bn.config_get(
        raw, "filters.no_entry_before", raw.get("no_new_trades_before", "09:35")
    )
    alert_deadline = bn.parse_time(str(deadline_str))
    max_quote_age = args.max_quote_age_seconds if args.max_quote_age_seconds is not None else int(raw.get("quote_stale_seconds", 90))
    min_coverage = args.min_coverage_pct if args.min_coverage_pct is not None else float(raw.get("min_constituent_coverage_pct", 70))

    underlying = str(raw.get("underlying_symbol", "NSE:NIFTYBANK-INDEX"))
    constituents = bn.parse_constituents(raw.get("constituents"))
    symbols = [underlying] + [c.fyers_symbol for c in constituents]

    latest_run: dict | None = None
    latest_success: dict | None = None
    quote_rows: dict[str, dict] = {}
    db_error: str | None = None
    try:
        with connect_readonly() as conn:
            latest_run, latest_success = fetch_latest_quote_ingestion(conn)
            quote_rows = fetch_quotes(conn, symbols)
    except Exception as exc:  # noqa: BLE001 - surface any DB issue as a data failure, never crash silently
        db_error = str(exc)

    report = build_report(
        raw,
        as_of=as_of,
        alert_deadline=alert_deadline,
        max_quote_age_seconds=max_quote_age,
        max_ingest_age_seconds=args.max_ingest_age_seconds,
        min_coverage_pct=min_coverage,
        strict=args.strict,
        latest_run=latest_run,
        latest_success=latest_success,
        quote_rows=quote_rows,
        db_error=db_error,
    )
    print(render_report(
        report,
        max_quote_age_seconds=max_quote_age,
        max_ingest_age_seconds=args.max_ingest_age_seconds,
        min_coverage_pct=min_coverage,
    ))
    raise SystemExit(report.exit_code)


if __name__ == "__main__":
    main()
