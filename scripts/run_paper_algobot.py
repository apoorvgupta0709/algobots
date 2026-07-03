#!/usr/bin/env python3
"""Phase 2A paper algobot for morning recommendation signals.

Safety stance:
- Paper trading only.
- No FYERS order placement, modification, or cancellation.
- Uses existing read-only recommendation signals and local quote snapshots.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import psycopg
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "paper_algobot.json"
DEFAULT_DATABASE_URL = "postgresql://hermes@127.0.0.1:55432/finance_tracker"
TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class BotConfig:
    strategy_version: str
    capital: Decimal
    max_risk_per_trade: Decimal
    max_weekly_loss: Decimal
    max_open_positions: int
    max_position_value: Decimal
    eligible_labels: tuple[str, ...]
    paper_only: bool
    live_orders_enabled: bool
    pending_entry_expiry_days: int
    max_holding_days: int
    trailing_stop_enabled: bool
    trail_activation_r: Decimal
    trail_distance_r: Decimal


@dataclass(frozen=True)
class SignalCandidate:
    signal_id: int
    signal_run_id: int
    symbol: str
    label: str
    score: Decimal
    entry_trigger: Decimal
    current_ltp: Decimal
    stop_loss: Decimal
    target: Decimal
    risks: list[str]
    local_context: dict[str, Any]


def money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"₹{value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def parse_money(value: Any) -> Decimal | None:
    """Parse values such as '₹1,579.00' or Decimal into Decimal."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "none", "null"}:
        return None
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned or cleaned in {"-", "."}:
        return None
    return Decimal(cleaned)


def week_start_utc(today: date | None = None) -> date:
    today = today or datetime.now(timezone.utc).date()
    return today - timedelta(days=today.weekday())


def size_quantity(entry_price: Decimal, stop_loss: Decimal, max_risk: Decimal, max_position_value: Decimal) -> tuple[int, Decimal, Decimal]:
    """Return quantity, actual max risk, position value for a long paper trade."""
    if entry_price <= 0 or stop_loss <= 0 or entry_price <= stop_loss:
        return 0, Decimal("0"), Decimal("0")
    risk_per_share = entry_price - stop_loss
    qty_by_risk = int((max_risk / risk_per_share).to_integral_value(rounding=ROUND_DOWN))
    qty_by_value = int((max_position_value / entry_price).to_integral_value(rounding=ROUND_DOWN))
    quantity = max(0, min(qty_by_risk, qty_by_value))
    actual_risk = (risk_per_share * quantity).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    position_value = (entry_price * quantity).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return quantity, actual_risk, position_value


def evaluate_exit(
    ltp: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    target: Decimal,
    quantity: int,
    *,
    active_stop: Decimal | None = None,
    now: datetime | None = None,
    entry_time: datetime | None = None,
    max_holding_days: int | None = None,
) -> tuple[str | None, Decimal | None, Decimal | None]:
    """Evaluate long paper-trade exits using conservative touch rules.

    ``stop_loss`` is the original stop. ``active_stop`` may be a trailed stop.
    Time stops use the current LTP because the paper system has no intraday fill book.
    """
    stop_to_use = active_stop or stop_loss
    stop_reason = "trailing_stop" if active_stop is not None and active_stop > stop_loss else "stop_loss"
    if ltp <= stop_to_use:
        pnl = (stop_to_use - entry_price) * quantity
        return stop_reason, stop_to_use, pnl.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if ltp >= target:
        pnl = (target - entry_price) * quantity
        return "target", target, pnl.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if now is not None and entry_time is not None and max_holding_days is not None:
        if now >= entry_time + timedelta(days=max_holding_days):
            pnl = (ltp - entry_price) * quantity
            return "time_stop", ltp, pnl.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return None, None, None


def next_trailing_stop(
    *,
    entry_price: Decimal,
    initial_stop: Decimal,
    current_stop: Decimal,
    highest_price: Decimal | None,
    ltp: Decimal,
    activation_r: Decimal,
    trail_r: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    """Return updated high-water mark, active stop, and whether the stop changed.

    The rule is long-only: after price reaches ``activation_r`` times initial risk,
    trail the stop ``trail_r`` times initial risk behind the high-water mark. The
    stop can only ratchet upward and never below breakeven once activated.
    """
    if entry_price <= 0 or initial_stop <= 0 or initial_stop >= entry_price:
        base_high = highest_price or ltp
        return max(base_high, ltp), current_stop, False
    risk_per_share = entry_price - initial_stop
    high = max(highest_price or entry_price, ltp)
    activation_price = entry_price + (risk_per_share * activation_r)
    if high < activation_price:
        return high, current_stop, False
    candidate_stop = high - (risk_per_share * trail_r)
    new_stop = max(current_stop, entry_price, candidate_stop).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
    old_stop = current_stop.quantize(SIX_PLACES, rounding=ROUND_HALF_UP)
    return high.quantize(SIX_PLACES, rounding=ROUND_HALF_UP), new_stop, new_stop > old_stop


def is_pending_entry_expired(created_at: datetime, now: datetime, *, expiry_days: int) -> bool:
    """Return true when a pending entry has expired without triggering."""
    return now >= created_at + timedelta(days=expiry_days)


def load_config(path: Path) -> BotConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("paper_only", True):
        raise SystemExit("Refusing to run: paper_only must be true for Phase 2A.")
    if data.get("live_orders_enabled", False):
        raise SystemExit("Refusing to run: live_orders_enabled must be false for Phase 2A.")
    labels = tuple(data.get("eligible_labels") or ["buy_candidate_research"])
    return BotConfig(
        strategy_version=str(data.get("strategy_version", "paper_algobot_v1")),
        capital=Decimal(str(data.get("capital", 5000))),
        max_risk_per_trade=Decimal(str(data.get("max_risk_per_trade", 50))),
        max_weekly_loss=Decimal(str(data.get("max_weekly_loss", 150))),
        max_open_positions=int(data.get("max_open_positions", 2)),
        max_position_value=Decimal(str(data.get("max_position_value", 2500))),
        eligible_labels=labels,
        paper_only=True,
        live_orders_enabled=False,
        pending_entry_expiry_days=int(data.get("pending_entry_expiry_days", 2)),
        max_holding_days=int(data.get("max_holding_days", 3)),
        trailing_stop_enabled=bool(data.get("trailing_stop_enabled", True)),
        trail_activation_r=Decimal(str(data.get("trail_activation_r", 1))),
        trail_distance_r=Decimal(str(data.get("trail_distance_r", 1))),
    )


def connect_db() -> psycopg.Connection:
    return psycopg.connect(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))


def json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except Exception:
            pass
        return [value]
    return [str(value)]


def json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def ensure_risk_state(cur: psycopg.Cursor, config: BotConfig) -> None:
    wk = week_start_utc()
    cur.execute(
        """
        insert into research.risk_state(week_start, capital, max_risk_per_trade, max_weekly_loss, max_open_positions, notes)
        values (%s, %s, %s, %s, %s, 'Paper algobot v1 risk state; live orders disabled.')
        on conflict(week_start) do update set
            capital = excluded.capital,
            max_risk_per_trade = excluded.max_risk_per_trade,
            max_weekly_loss = excluded.max_weekly_loss,
            max_open_positions = excluded.max_open_positions,
            updated_at = now()
        """,
        (wk, config.capital, config.max_risk_per_trade, config.max_weekly_loss, config.max_open_positions),
    )
    cur.execute(
        """
        update research.risk_state
        set realized_pnl = coalesce((
            select sum(realized_pnl)
            from research.paper_trades
            where status = 'closed'
              and exit_time >= %s::date
              and exit_time < (%s::date + interval '7 days')
        ), 0), updated_at = now()
        where week_start = %s
        """,
        (wk, wk, wk),
    )


def latest_signal_run(cur: psycopg.Cursor) -> int:
    cur.execute(
        """
        select signal_run_id
        from research.signal_runs
        where status = 'success'
        order by signal_run_id desc
        limit 1
        """
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit("No successful signal run found. Run morning recommendations first.")
    return int(row[0])


def fetch_candidates(cur: psycopg.Cursor, signal_run_id: int, config: BotConfig) -> list[SignalCandidate]:
    cur.execute(
        """
        select s.signal_id, s.signal_run_id, s.symbol, s.label, s.score, s.stop_loss, s.target,
               s.risks, s.local_context, q.ltp
        from research.signals s
        left join market.quotes q on q.symbol = s.symbol
        where s.signal_run_id = %s
          and s.label = any(%s)
          and s.stop_loss is not null
          and s.target is not null
        order by s.score desc, s.signal_id asc
        """,
        (signal_run_id, list(config.eligible_labels)),
    )
    candidates: list[SignalCandidate] = []
    for row in cur.fetchall():
        local_context = json_dict(row[8])
        trigger = parse_money(local_context.get("ltp")) or parse_money(row[9])
        current_ltp = parse_money(row[9]) or trigger
        if trigger is None or current_ltp is None:
            continue
        candidates.append(
            SignalCandidate(
                signal_id=int(row[0]),
                signal_run_id=int(row[1]),
                symbol=str(row[2]),
                label=str(row[3]),
                score=Decimal(str(row[4])),
                entry_trigger=trigger,
                current_ltp=current_ltp,
                stop_loss=Decimal(str(row[5])),
                target=Decimal(str(row[6])),
                risks=json_list(row[7]),
                local_context=local_context,
            )
        )
    return candidates


def insert_event(cur: psycopg.Cursor, trade_id: int, event_type: str, price: Decimal | None, quantity: int | None, message: str, raw: dict[str, Any] | None = None) -> None:
    cur.execute(
        """
        insert into research.paper_trade_events(paper_trade_id, event_type, price, quantity, message, raw)
        values (%s, %s, %s, %s, %s, %s::jsonb)
        """,
        (trade_id, event_type, price, quantity, message, json.dumps(raw or {})),
    )


def create_from_signals(config: BotConfig, signal_run_id: int | None = None) -> list[str]:
    lines = ["## Paper Algobot Phase 2A", "Mode: create paper trades from latest morning signals", "Safety: paper only — no FYERS orders placed, modified, or cancelled.", ""]
    with connect_db() as conn:
        with conn.cursor() as cur:
            ensure_risk_state(cur, config)
            run_id = signal_run_id or latest_signal_run(cur)
            cur.execute("select count(*) from research.paper_trades where status in ('pending_entry', 'open')")
            open_count = int(cur.fetchone()[0])
            cur.execute("select realized_pnl from research.risk_state where week_start = %s", (week_start_utc(),))
            weekly_pnl = Decimal(str(cur.fetchone()[0]))
            if weekly_pnl <= -config.max_weekly_loss:
                lines.append(f"Weekly risk lockout active: paper P&L {money(weekly_pnl)} <= -{money(config.max_weekly_loss)}")
                return lines

            candidates = fetch_candidates(cur, run_id, config)
            lines.append(f"Signal run: {run_id}")
            lines.append(f"Eligible labels: {', '.join(config.eligible_labels)}")
            lines.append(f"Open/pending before run: {open_count}/{config.max_open_positions}")
            if not candidates:
                lines.append("No eligible paper-trade candidates found.")
                return lines

            for candidate in candidates:
                if open_count >= config.max_open_positions:
                    lines.append(f"- {candidate.symbol}: skipped — max open positions reached.")
                    continue
                if candidate.stop_loss >= candidate.entry_trigger or candidate.target <= candidate.entry_trigger:
                    lines.append(f"- {candidate.symbol}: skipped — invalid stop/target around entry trigger.")
                    continue
                quantity, actual_risk, position_value = size_quantity(
                    candidate.current_ltp,
                    candidate.stop_loss,
                    config.max_risk_per_trade,
                    config.max_position_value,
                )
                if quantity < 1:
                    lines.append(f"- {candidate.symbol}: skipped — quantity < 1 under risk rules.")
                    continue
                status = "open" if candidate.current_ltp >= candidate.entry_trigger else "pending_entry"
                entry_price = candidate.current_ltp if status == "open" else None
                cur.execute(
                    """
                    insert into research.paper_trades(
                        signal_id, signal_run_id, symbol, status, entry_trigger, entry_price, entry_time,
                        stop_loss, initial_stop_loss, highest_price, time_stop_at, target, quantity, position_value, max_risk, strategy_version, notes, raw
                    ) values (%s, %s, %s, %s, %s, %s, case when %s = 'open' then now() else null end,
                              %s, %s, %s, case when %s = 'open' then now() + (%s::text || ' days')::interval else null end,
                              %s, %s, %s, %s, %s, %s, %s::jsonb)
                    on conflict(signal_id) do nothing
                    returning paper_trade_id
                    """,
                    (
                        candidate.signal_id,
                        candidate.signal_run_id,
                        candidate.symbol,
                        status,
                        candidate.entry_trigger,
                        entry_price,
                        status,
                        candidate.stop_loss,
                        candidate.stop_loss,
                        entry_price or candidate.current_ltp,
                        status,
                        config.max_holding_days,
                        candidate.target,
                        quantity,
                        position_value,
                        actual_risk,
                        config.strategy_version,
                        "Created by paper algobot Phase 2A. Live orders disabled.",
                        json.dumps({"label": candidate.label, "score": str(candidate.score), "risks": candidate.risks, "local_context": candidate.local_context}),
                    ),
                )
                inserted = cur.fetchone()
                if not inserted:
                    lines.append(f"- {candidate.symbol}: already has a paper trade for this signal.")
                    continue
                trade_id = int(inserted[0])
                event_type = "paper_opened" if status == "open" else "paper_pending"
                insert_event(cur, trade_id, event_type, entry_price or candidate.current_ltp, quantity, f"{candidate.symbol} {status}; qty {quantity}; max risk {money(actual_risk)}; no live order.")
                open_count += 1
                lines.append(
                    f"- {candidate.symbol}: {status}; qty {quantity}; entry/ref {money(entry_price or candidate.entry_trigger)}; "
                    f"SL {money(candidate.stop_loss)}; target {money(candidate.target)}; max paper risk {money(actual_risk)}; value {money(position_value)}"
                )
    return lines


def monitor_open_trades(config: BotConfig, refresh_quotes: bool = False, quiet_no_change: bool = False) -> list[str]:
    header = ["## Paper Algobot Monitor", "Safety: paper only — no FYERS orders placed, modified, or cancelled.", ""]
    action_lines: list[str] = []
    unchanged_lines: list[str] = []
    with connect_db() as conn:
        with conn.cursor() as cur:
            ensure_risk_state(cur, config)
            cur.execute(
                """
                select t.paper_trade_id, t.symbol, t.status, t.entry_trigger, t.entry_price,
                       t.stop_loss, coalesce(t.initial_stop_loss, t.stop_loss) as initial_stop_loss,
                       t.target, t.quantity, q.ltp, t.highest_price, t.entry_time, t.created_at
                from research.paper_trades t
                left join market.quotes q on q.symbol = t.symbol
                where t.status in ('pending_entry', 'open')
                order by t.created_at asc
                """
            )
            trades = cur.fetchall()
            if not trades:
                if quiet_no_change:
                    return []
                return header + ["No open or pending paper trades."]
            symbols = [str(row[1]) for row in trades]
            if refresh_quotes:
                subprocess.run(
                    [sys.executable, str(PROJECT_ROOT / "scripts" / "ingest_fyers_quotes.py"), "--symbols", *symbols],
                    cwd=str(PROJECT_ROOT),
                    check=True,
                    env={**os.environ, "FYERS_LOG_PATH": os.getenv("FYERS_LOG_PATH", "/tmp/")},
                )
                cur.execute(
                    """
                    select t.paper_trade_id, t.symbol, t.status, t.entry_trigger, t.entry_price,
                           t.stop_loss, coalesce(t.initial_stop_loss, t.stop_loss) as initial_stop_loss,
                           t.target, t.quantity, q.ltp, t.highest_price, t.entry_time, t.created_at
                    from research.paper_trades t
                    left join market.quotes q on q.symbol = t.symbol
                    where t.status in ('pending_entry', 'open')
                    order by t.created_at asc
                    """
                )
                trades = cur.fetchall()
            now = datetime.now(timezone.utc)
            for trade_id, symbol, status, trigger, entry, stop, initial_stop, target, quantity, ltp, highest_price, entry_time, created_at in trades:
                ltp_dec = parse_money(ltp)
                if ltp_dec is None:
                    unchanged_lines.append(f"- {symbol}: no latest quote; unchanged.")
                    continue
                trigger_dec = Decimal(str(trigger))
                stop_dec = Decimal(str(stop))
                initial_stop_dec = Decimal(str(initial_stop))
                target_dec = Decimal(str(target))
                qty = int(quantity)
                if status == "pending_entry":
                    if is_pending_entry_expired(created_at, now, expiry_days=config.pending_entry_expiry_days):
                        cur.execute(
                            """
                            update research.paper_trades
                            set status='cancelled', exit_reason='pending_entry_expired', updated_at=now()
                            where paper_trade_id=%s
                            """,
                            (trade_id,),
                        )
                        insert_event(cur, int(trade_id), "paper_cancelled_expired", ltp_dec, qty, f"Pending entry expired after {config.pending_entry_expiry_days} days; no live order.")
                        action_lines.append(f"- {symbol}: cancelled pending entry — not triggered within {config.pending_entry_expiry_days} days")
                    elif ltp_dec >= trigger_dec:
                        cur.execute(
                            """
                            update research.paper_trades
                            set status='open', entry_price=%s, entry_time=now(), time_stop_at=now() + (%s::text || ' days')::interval,
                                position_value=%s, highest_price=%s, updated_at=now()
                            where paper_trade_id=%s
                            """,
                            (ltp_dec, config.max_holding_days, (ltp_dec * qty).quantize(TWO_PLACES), ltp_dec, trade_id),
                        )
                        insert_event(cur, int(trade_id), "paper_opened", ltp_dec, qty, f"Pending entry triggered at {money(ltp_dec)}; no live order.")
                        action_lines.append(f"- {symbol}: pending -> open at {money(ltp_dec)}; qty {qty}")
                    else:
                        unchanged_lines.append(f"- {symbol}: pending; LTP {money(ltp_dec)} below trigger {money(trigger_dec)}")
                    continue
                entry_dec = Decimal(str(entry))
                high_dec = parse_money(highest_price) or entry_dec
                active_stop_dec = stop_dec
                if config.trailing_stop_enabled:
                    new_high, new_stop, stop_changed = next_trailing_stop(
                        entry_price=entry_dec,
                        initial_stop=initial_stop_dec,
                        current_stop=stop_dec,
                        highest_price=high_dec,
                        ltp=ltp_dec,
                        activation_r=config.trail_activation_r,
                        trail_r=config.trail_distance_r,
                    )
                    if stop_changed:
                        cur.execute(
                            """
                            update research.paper_trades
                            set highest_price=%s, stop_loss=%s, trail_activated=true, updated_at=now()
                            where paper_trade_id=%s
                            """,
                            (new_high, new_stop, trade_id),
                        )
                        insert_event(cur, int(trade_id), "paper_trailing_stop_raised", new_stop, qty, f"Trailing stop raised to {money(new_stop)}; no live order.", {"highest_price": str(new_high)})
                        action_lines.append(f"- {symbol}: trailing SL raised to {money(new_stop)} after high-water {money(new_high)}")
                    elif new_high > high_dec:
                        cur.execute(
                            """
                            update research.paper_trades
                            set highest_price=%s, updated_at=now()
                            where paper_trade_id=%s
                            """,
                            (new_high, trade_id),
                        )
                    active_stop_dec = new_stop
                reason, exit_price, pnl = evaluate_exit(
                    ltp_dec,
                    entry_dec,
                    initial_stop_dec,
                    target_dec,
                    qty,
                    active_stop=active_stop_dec,
                    now=now,
                    entry_time=entry_time,
                    max_holding_days=config.max_holding_days,
                )
                if reason:
                    cur.execute(
                        """
                        update research.paper_trades
                        set status='closed', exit_price=%s, exit_time=now(), realized_pnl=%s,
                            exit_reason=%s, updated_at=now()
                        where paper_trade_id=%s
                        """,
                        (exit_price, pnl, reason, trade_id),
                    )
                    insert_event(cur, int(trade_id), f"paper_closed_{reason}", exit_price, qty, f"Closed by {reason}; paper P&L {money(pnl)}; no live order.")
                    action_lines.append(f"- {symbol}: closed by {reason}; exit {money(exit_price)}; paper P&L {money(pnl)}")
                else:
                    unrealized = ((ltp_dec - entry_dec) * qty).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
                    unchanged_lines.append(f"- {symbol}: open; LTP {money(ltp_dec)}; unrealized paper P&L {money(unrealized)}; active SL {money(active_stop_dec)}; target {money(target_dec)}")
            ensure_risk_state(cur, config)
    if action_lines:
        return header + action_lines + ([""] + unchanged_lines if unchanged_lines and not quiet_no_change else [])
    if quiet_no_change:
        return []
    return header + unchanged_lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=["create", "monitor"], default="create")
    parser.add_argument("--signal-run-id", type=int)
    parser.add_argument("--refresh-quotes", action="store_true", help="Read-only FYERS quote refresh before monitoring open paper trades.")
    parser.add_argument("--quiet-no-change", action="store_true", help="Print nothing when monitor finds no entry/exit/status change; useful for cron watchdogs.")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.mode == "create":
        lines = create_from_signals(config, args.signal_run_id)
    else:
        lines = monitor_open_trades(config, args.refresh_quotes, args.quiet_no_change)
    if lines:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
