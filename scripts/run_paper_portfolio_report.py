#!/usr/bin/env python3
"""Track day-on-day paper portfolio growth for the Phase 2A algobot.

Safety stance:
- Paper portfolio accounting only.
- No FYERS order placement, modification, cancellation, or exit calls.
- Computes equity from research.paper_trades plus market.quotes.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "postgresql://" + "hermes" + "@" + "127.0.0.1" + ":55432" + "/finance_tracker"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"
TWO_PLACES = Decimal("0.01")

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class PortfolioRun:
    portfolio_run_id: int
    name: str
    start_date: date
    starting_capital: Decimal


def money(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"₹{value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def compute_equity(starting_capital: Decimal, realized_pnl: Decimal, unrealized_pnl: Decimal) -> Decimal:
    return (starting_capital + realized_pnl + unrealized_pnl).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def connect_db() -> psycopg.Connection:
    return psycopg.connect(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))


def apply_migrations() -> None:
    for migration in sorted((PROJECT_ROOT / "migrations").glob("*.sql")):
        subprocess.run(
            [str(PROJECT_ROOT / "scripts" / "psql.sh"), "-h", "127.0.0.1", "-p", "55432", "-d", "finance_tracker", "-f", str(migration)],
            cwd=str(PROJECT_ROOT),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )


def get_active_run(cur: psycopg.Cursor, name: str | None = None) -> PortfolioRun:
    if name:
        cur.execute(
            """
            select portfolio_run_id, name, start_date, starting_capital
            from research.paper_portfolio_runs
            where name = %s
            """,
            (name,),
        )
    else:
        cur.execute(
            """
            select portfolio_run_id, name, start_date, starting_capital
            from research.paper_portfolio_runs
            where active = true
            order by start_date desc, portfolio_run_id desc
            limit 1
            """
        )
    row = cur.fetchone()
    if not row:
        raise SystemExit("No active paper portfolio run found. Run --mode init first.")
    return PortfolioRun(int(row[0]), str(row[1]), row[2], Decimal(str(row[3])))


def init_portfolio(name: str, starting_capital: Decimal, start_date: date, cancel_open_before_start: bool) -> list[str]:
    lines = ["## Paper Portfolio Init", "Safety: paper accounting only — no live orders placed.", ""]
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute("update research.paper_portfolio_runs set active=false, updated_at=now() where active=true")
            cur.execute(
                """
                insert into research.paper_portfolio_runs(name, start_date, starting_capital, active, notes)
                values (%s, %s, %s, true, '₹5,000 fictitious paper portfolio baseline; live orders disabled.')
                on conflict(name) do update set
                    start_date = excluded.start_date,
                    starting_capital = excluded.starting_capital,
                    active = true,
                    updated_at = now()
                returning portfolio_run_id
                """,
                (name, start_date, starting_capital),
            )
            run_id = int(cur.fetchone()[0])
            lines.append(f"Portfolio run: {name} (id {run_id})")
            lines.append(f"Start date: {start_date.isoformat()}")
            lines.append(f"Starting capital: {money(starting_capital)}")
            if cancel_open_before_start:
                cur.execute(
                    """
                    select paper_trade_id, symbol, status, quantity
                    from research.paper_trades
                    where status in ('pending_entry', 'open')
                      and created_at::date < %s
                    order by paper_trade_id
                    """,
                    (start_date,),
                )
                stale = cur.fetchall()
                for trade_id, symbol, status, qty in stale:
                    cur.execute(
                        """
                        update research.paper_trades
                        set status='cancelled', exit_reason='paper_portfolio_baseline_reset', updated_at=now(),
                            notes = coalesce(notes, '') || ' Cancelled for new paper portfolio baseline.'
                        where paper_trade_id=%s
                        """,
                        (trade_id,),
                    )
                    cur.execute(
                        """
                        insert into research.paper_trade_events(paper_trade_id, event_type, price, quantity, message, raw)
                        values (%s, 'paper_cancelled_baseline_reset', null, %s, %s, %s::jsonb)
                        """,
                        (
                            trade_id,
                            qty,
                            f"{symbol} {status} cancelled to start new {money(starting_capital)} paper portfolio on {start_date.isoformat()}; no live order.",
                            json.dumps({"portfolio_run_id": run_id, "portfolio_name": name}),
                        ),
                    )
                lines.append(f"Pre-baseline open/pending paper trades cancelled: {len(stale)}")
    return lines


def snapshot_portfolio(name: str | None, output: Path | None = None, print_report: bool = False) -> list[str]:
    today = datetime.now(timezone.utc).date()
    with connect_db() as conn:
        with conn.cursor() as cur:
            run = get_active_run(cur, name)
            cur.execute(
                """
                select
                    coalesce(sum(case when status='closed' then realized_pnl else 0 end), 0) as realized_pnl,
                    coalesce(sum(case when status='open' and entry_price is not null and q.ltp is not null then (q.ltp - entry_price) * quantity else 0 end), 0) as unrealized_pnl,
                    count(*) filter (where status='open') as open_positions,
                    count(*) filter (where status='pending_entry') as pending_positions,
                    count(*) filter (where status='closed') as closed_positions
                from research.paper_trades t
                left join market.quotes q on q.symbol=t.symbol
                where t.created_at::date >= %s
                  and t.status in ('pending_entry', 'open', 'closed')
                """,
                (run.start_date,),
            )
            realized, unrealized, open_count, pending_count, closed_count = cur.fetchone()
            realized_dec = Decimal(str(realized or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            unrealized_dec = Decimal(str(unrealized or 0)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            equity = compute_equity(run.starting_capital, realized_dec, unrealized_dec)
            cur.execute(
                """
                select t.paper_trade_id, t.symbol, t.status, t.entry_price, t.entry_trigger, t.stop_loss, t.target,
                       t.quantity, q.ltp,
                       case when t.status='open' and t.entry_price is not null and q.ltp is not null then (q.ltp - t.entry_price) * t.quantity else null end as unrealized_pnl
                from research.paper_trades t
                left join market.quotes q on q.symbol=t.symbol
                where t.created_at::date >= %s
                  and t.status in ('pending_entry', 'open')
                order by t.created_at asc
                """,
                (run.start_date,),
            )
            positions = cur.fetchall()
            raw = {
                "portfolio_name": run.name,
                "start_date": run.start_date.isoformat(),
                "positions": [
                    {
                        "paper_trade_id": int(row[0]),
                        "symbol": str(row[1]),
                        "status": str(row[2]),
                        "entry_price": str(row[3]) if row[3] is not None else None,
                        "entry_trigger": str(row[4]) if row[4] is not None else None,
                        "stop_loss": str(row[5]) if row[5] is not None else None,
                        "target": str(row[6]) if row[6] is not None else None,
                        "quantity": int(row[7]),
                        "ltp": str(row[8]) if row[8] is not None else None,
                        "unrealized_pnl": str(row[9]) if row[9] is not None else None,
                    }
                    for row in positions
                ],
            }
            cur.execute(
                """
                insert into research.paper_portfolio_daily_snapshots(
                    portfolio_run_id, snapshot_date, starting_capital, realized_pnl, unrealized_pnl,
                    equity, open_positions, pending_positions, closed_positions, raw
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                on conflict(portfolio_run_id, snapshot_date) do update set
                    starting_capital=excluded.starting_capital,
                    realized_pnl=excluded.realized_pnl,
                    unrealized_pnl=excluded.unrealized_pnl,
                    equity=excluded.equity,
                    open_positions=excluded.open_positions,
                    pending_positions=excluded.pending_positions,
                    closed_positions=excluded.closed_positions,
                    raw=excluded.raw,
                    updated_at=now()
                returning snapshot_id
                """,
                (
                    run.portfolio_run_id,
                    today,
                    run.starting_capital,
                    realized_dec,
                    unrealized_dec,
                    equity,
                    int(open_count or 0),
                    int(pending_count or 0),
                    int(closed_count or 0),
                    json.dumps(raw),
                ),
            )
            snapshot_id = int(cur.fetchone()[0])
            cur.execute(
                """
                select snapshot_date, equity, realized_pnl, unrealized_pnl, open_positions, pending_positions, closed_positions
                from research.paper_portfolio_daily_snapshots
                where portfolio_run_id=%s
                order by snapshot_date
                """,
                (run.portfolio_run_id,),
            )
            history = cur.fetchall()

    lines = [
        "## Paper Portfolio Snapshot",
        "Safety: paper accounting only — no live orders placed.",
        "",
        f"Portfolio: {run.name}",
        f"Snapshot ID: {snapshot_id}",
        f"Start date: {run.start_date.isoformat()}",
        f"Starting capital: {money(run.starting_capital)}",
        f"Current equity: {money(equity)}",
        f"Day/account P&L: {money(equity - run.starting_capital)}",
        f"Realized P&L: {money(realized_dec)}",
        f"Unrealized P&L: {money(unrealized_dec)}",
        f"Open / pending / closed positions: {int(open_count or 0)} / {int(pending_count or 0)} / {int(closed_count or 0)}",
        "",
        "## Open or pending positions",
    ]
    if not positions:
        lines.append("- None yet. The 11:00 IST recommendation job will create paper trades only if eligible signals pass risk rules.")
    else:
        for row in positions:
            pnl = Decimal(str(row[9])).quantize(TWO_PLACES, rounding=ROUND_HALF_UP) if row[9] is not None else None
            lines.append(
                f"- {row[1]}: {row[2]}; qty {row[7]}; entry {money(Decimal(str(row[3])) if row[3] is not None else None)}; "
                f"trigger {money(Decimal(str(row[4])))}; LTP {money(Decimal(str(row[8])) if row[8] is not None else None)}; "
                f"SL {money(Decimal(str(row[5])))}; target {money(Decimal(str(row[6])))}; unrealized {money(pnl)}"
            )
    lines.extend(["", "## Day-on-day history"])
    for snap_date, hist_equity, hist_realized, hist_unrealized, hist_open, hist_pending, hist_closed in history:
        lines.append(
            f"- {snap_date}: equity {money(Decimal(str(hist_equity)))}; realized {money(Decimal(str(hist_realized)))}; "
            f"unrealized {money(Decimal(str(hist_unrealized)))}; open/pending/closed {hist_open}/{hist_pending}/{hist_closed}"
        )
    text = "\n".join(lines) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    if print_report:
        print(text)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Track paper portfolio growth; paper-only, no live orders.")
    parser.add_argument("--mode", choices=["init", "snapshot"], default="snapshot")
    parser.add_argument("--name", help="Portfolio run name. Defaults to active run for snapshots.")
    parser.add_argument("--starting-capital", type=Decimal, default=Decimal("5000"))
    parser.add_argument("--start-date", type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    parser.add_argument("--cancel-open-before-start", action="store_true", help="Cancel pre-baseline open/pending paper trades without deleting audit history.")
    parser.add_argument("--apply-migrations", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--print", action="store_true")
    args = parser.parse_args()

    if args.apply_migrations:
        apply_migrations()
    if args.mode == "init":
        name = args.name or f"paper_5000_{args.start_date.isoformat()}"
        lines = init_portfolio(name, args.starting_capital, args.start_date, args.cancel_open_before_start)
        print("\n".join(lines))
    else:
        snapshot_portfolio(args.name, args.output, args.print)


if __name__ == "__main__":
    main()
