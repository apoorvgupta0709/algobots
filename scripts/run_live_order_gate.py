#!/usr/bin/env python3
"""Phase 2B live-order gate scaffold.

Safety stance:
- Default is dry-run only: live_orders_enabled=false and kill_switch_enabled=true.
- This script creates review/audit records and exact-confirmation approvals.
- It does not call FYERS order placement, modification, or cancellation APIs.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "live_order_gate.json"
DEFAULT_DATABASE_URL = "postgresql://" + "hermes" + "@" + "127.0.0.1" + ":55432" + "/" + "finance_tracker"
TWO_PLACES = Decimal("0.01")

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class GateConfig:
    live_orders_enabled: bool
    kill_switch_enabled: bool
    max_capital: Decimal
    max_risk_per_trade: Decimal
    max_daily_loss: Decimal
    max_weekly_loss: Decimal
    max_open_positions: int
    allowed_product_types: tuple[str, ...]
    allowed_order_types: tuple[str, ...]


@dataclass(frozen=True)
class TradeIdeaDraft:
    symbol: str
    side: str
    quantity: Decimal
    order_type: str
    price: Decimal | None
    trigger_price: Decimal | None
    product_type: str
    validity: str
    stop_loss: Decimal | None
    target_price: Decimal | None
    rationale: str
    max_loss_amount: Decimal | None


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    action: str
    reasons: tuple[str, ...]


def q2(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money(value: Decimal | None) -> str:
    value = q2(value)
    if value is None:
        return "n/a"
    return f"₹{value:,.2f}"


def fmt_qty(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def build_confirmation_text(idea: TradeIdeaDraft) -> str:
    price = f" @ {money(idea.price)}" if idea.price is not None else ""
    trigger = f" trigger {money(idea.trigger_price)}" if idea.trigger_price is not None else ""
    return (
        f"APPROVE LIVE REVIEW ONLY: {idea.side} {fmt_qty(idea.quantity)} {idea.symbol} {idea.order_type}{price}{trigger}; "
        f"product {idea.product_type}; validity {idea.validity}; max loss {money(idea.max_loss_amount)}; "
        f"SL {money(idea.stop_loss)}; target {money(idea.target_price)}"
    )


def validate_confirmation_text(text: str, idea: TradeIdeaDraft) -> bool:
    return text == build_confirmation_text(idea)


def evaluate_gate(
    config: GateConfig,
    idea: TradeIdeaDraft,
    *,
    open_positions: int,
    realized_pnl_today: Decimal,
    realized_pnl_week: Decimal,
    approval_status: str | None,
    confirmation_text: str | None,
) -> GateDecision:
    reasons: list[str] = []
    if config.kill_switch_enabled:
        reasons.append("kill_switch_enabled")
    if not config.live_orders_enabled:
        reasons.append("live_orders_enabled_false")
    if idea.product_type not in config.allowed_product_types:
        reasons.append("product_type_not_allowed")
    if idea.order_type not in config.allowed_order_types:
        reasons.append("order_type_not_allowed")
    if open_positions >= config.max_open_positions:
        reasons.append("max_open_positions_reached")
    order_value = (idea.price or Decimal("0")) * idea.quantity
    if order_value > config.max_capital:
        reasons.append("order_value_exceeds_capital")
    if idea.max_loss_amount is None or idea.max_loss_amount > config.max_risk_per_trade:
        reasons.append("max_loss_exceeds_config")
    if realized_pnl_today <= -config.max_daily_loss:
        reasons.append("daily_loss_limit_reached")
    if realized_pnl_week <= -config.max_weekly_loss:
        reasons.append("weekly_loss_limit_reached")
    if approval_status != "approved":
        reasons.append("approval_missing_or_not_approved")
    if not confirmation_text or not validate_confirmation_text(confirmation_text, idea):
        reasons.append("confirmation_text_mismatch")
    if reasons:
        return GateDecision(False, "dry_run_only", tuple(reasons))
    return GateDecision(True, "ready_for_manual_live_execution", ())


def load_config(path: Path) -> GateConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    return GateConfig(
        live_orders_enabled=bool(data.get("live_orders_enabled", False)),
        kill_switch_enabled=bool(data.get("kill_switch_enabled", True)),
        max_capital=Decimal(str(data.get("max_capital", 5000))),
        max_risk_per_trade=Decimal(str(data.get("max_risk_per_trade", 50))),
        max_daily_loss=Decimal(str(data.get("max_daily_loss", 100))),
        max_weekly_loss=Decimal(str(data.get("max_weekly_loss", 150))),
        max_open_positions=int(data.get("max_open_positions", 1)),
        allowed_product_types=tuple(data.get("allowed_product_types") or ["CNC"]),
        allowed_order_types=tuple(data.get("allowed_order_types") or ["LIMIT"]),
    )


def connect_db() -> psycopg.Connection:
    return psycopg.connect(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))


def apply_migrations(conn: psycopg.Connection) -> None:
    for migration in [
        "001_trading_research_schemas.sql",
        "004_paper_algobot.sql",
        "005_algobot_phase2_live_gate.sql",
    ]:
        with conn.cursor() as cur:
            cur.execute((PROJECT_ROOT / "migrations" / migration).read_text(encoding="utf-8"))
    conn.commit()


def draft_from_row(row: tuple[Any, ...], *, product_type: str, order_type: str) -> TradeIdeaDraft:
    _, symbol, quantity, entry_price, stop_loss, target, max_risk, notes = row
    price = Decimal(str(entry_price))
    return TradeIdeaDraft(
        symbol=str(symbol),
        side="BUY",
        quantity=Decimal(str(quantity)),
        order_type=order_type,
        price=price,
        trigger_price=None,
        product_type=product_type,
        validity="DAY",
        stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
        target_price=Decimal(str(target)) if target is not None else None,
        rationale=str(notes or "Paper algobot trade promoted for live review."),
        max_loss_amount=Decimal(str(max_risk)) if max_risk is not None else None,
    )


def draft_from_idea_row(row: tuple[Any, ...]) -> TradeIdeaDraft:
    return TradeIdeaDraft(
        symbol=str(row[1]),
        side=str(row[2]),
        quantity=Decimal(str(row[3])),
        order_type=str(row[4]),
        price=Decimal(str(row[5])) if row[5] is not None else None,
        trigger_price=Decimal(str(row[6])) if row[6] is not None else None,
        product_type=str(row[7]),
        validity=str(row[8]),
        stop_loss=Decimal(str(row[9])) if row[9] is not None else None,
        target_price=Decimal(str(row[10])) if row[10] is not None else None,
        rationale=str(row[11]),
        max_loss_amount=Decimal(str((row[12] or {}).get("max_loss_amount", 0))),
    )


def create_ideas_from_paper(config_path: Path, limit: int) -> list[str]:
    config_data = json.loads(config_path.read_text(encoding="utf-8"))
    product_type = str(config_data.get("default_product_type", "CNC"))
    order_type = str(config_data.get("default_order_type", "LIMIT"))
    lines = ["## Phase 2B Live-Order Gate", "Mode: create live-review ideas from open paper trades", "Safety: dry-run/live-review only — no FYERS orders placed.", ""]
    with connect_db() as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                select t.paper_trade_id, t.symbol, t.quantity, t.entry_price, t.stop_loss, t.target, t.max_risk, t.notes
                from research.paper_trades t
                where t.status = 'open'
                  and not exists (
                    select 1 from trading.trade_ideas i
                    where i.source_snapshot->>'paper_trade_id' = t.paper_trade_id::text
                      and i.status in ('generated', 'review', 'approved')
                  )
                order by t.created_at desc
                limit %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            if not rows:
                return lines + ["No open paper trades available for live-review idea creation."]
            for row in rows:
                paper_trade_id = int(row[0])
                draft = draft_from_row(row, product_type=product_type, order_type=order_type)
                cur.execute(
                    """
                    insert into trading.trade_ideas(
                        symbol, side, quantity, order_type, price, product_type, validity,
                        stop_loss, target_price, rationale, source_snapshot, risk_snapshot, status, expires_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'review', now() + interval '1 day')
                    returning idea_id
                    """,
                    (
                        draft.symbol,
                        draft.side,
                        draft.quantity,
                        draft.order_type,
                        draft.price,
                        draft.product_type,
                        draft.validity,
                        draft.stop_loss,
                        draft.target_price,
                        draft.rationale,
                        json.dumps({"source": "paper_algobot", "paper_trade_id": paper_trade_id}),
                        json.dumps({"max_loss_amount": str(draft.max_loss_amount), "gate": "manual_review_required"}),
                    ),
                )
                idea_id = int(cur.fetchone()[0])
                lines.extend([
                    f"- Idea {idea_id}: {draft.symbol} {draft.side} {fmt_qty(draft.quantity)} {draft.order_type} {money(draft.price)}",
                    f"  - Confirmation required: `{build_confirmation_text(draft)}`",
                ])
    return lines


def fetch_idea(cur: psycopg.Cursor, idea_id: int) -> tuple[tuple[Any, ...], TradeIdeaDraft]:
    cur.execute(
        """
        select idea_id, symbol, side, quantity, order_type, price, trigger_price, product_type,
               validity, stop_loss, target_price, rationale, risk_snapshot
        from trading.trade_ideas
        where idea_id = %s
        """,
        (idea_id,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"No trade idea found for idea_id={idea_id}")
    return row, draft_from_idea_row(row)


def show_confirmation(idea_id: int) -> list[str]:
    with connect_db() as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            _, draft = fetch_idea(cur, idea_id)
    return [
        "## Live-review confirmation text",
        "No order will be placed by showing this text.",
        build_confirmation_text(draft),
    ]


def approve_idea(idea_id: int, approved_by: str, confirmation_text: str) -> list[str]:
    with connect_db() as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            _, draft = fetch_idea(cur, idea_id)
            expected = build_confirmation_text(draft)
            if confirmation_text != expected:
                raise SystemExit("Confirmation text mismatch. Approval not recorded.")
            cur.execute(
                """
                insert into trading.approvals(
                    idea_id, approved_by, confirmation_text, symbol, side, quantity, order_type,
                    price, trigger_price, product_type, validity, max_loss_amount, exit_plan,
                    expires_at, raw
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + interval '1 day', %s::jsonb)
                returning approval_id
                """,
                (
                    idea_id,
                    approved_by,
                    confirmation_text,
                    draft.symbol,
                    draft.side,
                    draft.quantity,
                    draft.order_type,
                    draft.price,
                    draft.trigger_price,
                    draft.product_type,
                    draft.validity,
                    draft.max_loss_amount,
                    f"SL {money(draft.stop_loss)}; target {money(draft.target_price)}; manual monitoring required.",
                    json.dumps({"gate": "phase_2b_live_review_only"}),
                ),
            )
            approval_id = int(cur.fetchone()[0])
            cur.execute("update trading.trade_ideas set status='approved' where idea_id=%s", (idea_id,))
    return [f"Approval recorded: {approval_id}", "Safety: approval is for live review gate only; no broker order placed."]


def current_risk_context(cur: psycopg.Cursor) -> tuple[int, Decimal, Decimal]:
    today = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=today.weekday())
    cur.execute("select count(*) from research.paper_trades where status='open'")
    open_positions = int(cur.fetchone()[0])
    cur.execute(
        """
        select coalesce(sum(realized_pnl), 0)
        from research.paper_trades
        where status='closed' and exit_time >= %s::date and exit_time < (%s::date + interval '1 day')
        """,
        (today, today),
    )
    day_pnl = Decimal(str(cur.fetchone()[0]))
    cur.execute(
        """
        select coalesce(sum(realized_pnl), 0)
        from research.paper_trades
        where status='closed' and exit_time >= %s::date and exit_time < (%s::date + interval '7 days')
        """,
        (week_start, week_start),
    )
    week_pnl = Decimal(str(cur.fetchone()[0]))
    return open_positions, day_pnl, week_pnl


def dry_run_approval(config: GateConfig, approval_id: int) -> list[str]:
    with connect_db() as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                select a.approval_id, a.idea_id, a.status, a.confirmation_text,
                       i.symbol, i.side, i.quantity, i.order_type, i.price, i.trigger_price,
                       i.product_type, i.validity, i.stop_loss, i.target_price, i.rationale,
                       a.max_loss_amount
                from trading.approvals a
                join trading.trade_ideas i on i.idea_id = a.idea_id
                where a.approval_id = %s
                """,
                (approval_id,),
            )
            row = cur.fetchone()
            if not row:
                raise SystemExit(f"No approval found for approval_id={approval_id}")
            draft = TradeIdeaDraft(
                symbol=str(row[4]), side=str(row[5]), quantity=Decimal(str(row[6])), order_type=str(row[7]),
                price=Decimal(str(row[8])) if row[8] is not None else None,
                trigger_price=Decimal(str(row[9])) if row[9] is not None else None,
                product_type=str(row[10]), validity=str(row[11]),
                stop_loss=Decimal(str(row[12])) if row[12] is not None else None,
                target_price=Decimal(str(row[13])) if row[13] is not None else None,
                rationale=str(row[14]), max_loss_amount=Decimal(str(row[15])) if row[15] is not None else None,
            )
            open_positions, day_pnl, week_pnl = current_risk_context(cur)
            decision = evaluate_gate(
                config,
                draft,
                open_positions=open_positions,
                realized_pnl_today=day_pnl,
                realized_pnl_week=week_pnl,
                approval_status=str(row[2]),
                confirmation_text=str(row[3]),
            )
            api_status = "ready_manual_review" if decision.allowed else "blocked_by_gate"
            cur.execute(
                """
                insert into trading.execution_log(
                    approval_id, idea_id, symbol, side, quantity, order_type, price, trigger_price,
                    product_type, validity, action, api_status, api_message, raw
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'dry_run', %s, %s, %s::jsonb)
                returning execution_id
                """,
                (
                    approval_id,
                    int(row[1]),
                    draft.symbol,
                    draft.side,
                    draft.quantity,
                    draft.order_type,
                    draft.price,
                    draft.trigger_price,
                    draft.product_type,
                    draft.validity,
                    api_status,
                    ", ".join(decision.reasons) if decision.reasons else "All gate checks passed for manual live review. Broker order still not placed.",
                    json.dumps({"decision": decision.__dict__, "open_positions": open_positions, "day_pnl": str(day_pnl), "week_pnl": str(week_pnl)}),
                ),
            )
            execution_id = int(cur.fetchone()[0])
    lines = [
        "## Phase 2B Gate Dry Run",
        f"Execution log ID: {execution_id}",
        f"Decision: {decision.action}",
        "Safety: no FYERS order placed, modified, or cancelled.",
    ]
    if decision.reasons:
        lines.append("Blocked reasons: " + ", ".join(decision.reasons))
    return lines


def status(config: GateConfig) -> list[str]:
    return [
        "## Live-Order Gate Status",
        f"live_orders_enabled: {config.live_orders_enabled}",
        f"kill_switch_enabled: {config.kill_switch_enabled}",
        f"max_capital: {money(config.max_capital)}",
        f"max_risk_per_trade: {money(config.max_risk_per_trade)}",
        "Broker order placement code: disabled/not implemented in this Phase 2B scaffold.",
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 2B live-order gate scaffold; dry-run only by default")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=["status", "create-ideas", "show-confirmation", "approve", "dry-run"], default="status")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--idea-id", type=int)
    parser.add_argument("--approval-id", type=int)
    parser.add_argument("--approved-by", default="Apoorv")
    parser.add_argument("--confirmation-text")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if args.mode == "status":
        lines = status(config)
    elif args.mode == "create-ideas":
        lines = create_ideas_from_paper(args.config, args.limit)
    elif args.mode == "show-confirmation":
        if args.idea_id is None:
            raise SystemExit("--idea-id is required")
        lines = show_confirmation(args.idea_id)
    elif args.mode == "approve":
        if args.idea_id is None or not args.confirmation_text:
            raise SystemExit("--idea-id and --confirmation-text are required")
        lines = approve_idea(args.idea_id, args.approved_by, args.confirmation_text)
    else:
        if args.approval_id is None:
            raise SystemExit("--approval-id is required")
        lines = dry_run_approval(config, args.approval_id)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
