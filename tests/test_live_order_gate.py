from decimal import Decimal
from pathlib import Path
import json
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_live_order_gate import (
    GateConfig,
    GateDecision,
    TradeIdeaDraft,
    build_confirmation_text,
    draft_from_idea_row,
    evaluate_gate,
    load_config,
    validate_confirmation_text,
)


def sample_idea() -> TradeIdeaDraft:
    return TradeIdeaDraft(
        symbol="NSE:SBIN-EQ",
        side="BUY",
        quantity=Decimal("4"),
        order_type="LIMIT",
        price=Decimal("750.50"),
        trigger_price=None,
        product_type="CNC",
        validity="DAY",
        stop_loss=Decimal("735.50"),
        target_price=Decimal("780.50"),
        rationale="Paper signal promoted for live review.",
        max_loss_amount=Decimal("60.00"),
    )


def test_confirmation_text_is_exact_and_mask_safe():
    idea = sample_idea()
    expected = (
        "APPROVE LIVE REVIEW ONLY: BUY 4 NSE:SBIN-EQ LIMIT @ ₹750.50; "
        "product CNC; validity DAY; max loss ₹60.00; SL ₹735.50; target ₹780.50"
    )
    assert build_confirmation_text(idea) == expected
    assert validate_confirmation_text(expected, idea) is True
    assert validate_confirmation_text(expected.lower(), idea) is False


def test_gate_refuses_when_kill_switch_or_live_disabled_even_with_approval():
    idea = sample_idea()
    config = GateConfig(
        live_orders_enabled=False,
        kill_switch_enabled=True,
        max_capital=Decimal("5000"),
        max_risk_per_trade=Decimal("50"),
        max_daily_loss=Decimal("100"),
        max_weekly_loss=Decimal("150"),
        max_open_positions=1,
        allowed_product_types=("CNC",),
        allowed_order_types=("LIMIT",),
    )
    decision = evaluate_gate(
        config,
        idea,
        open_positions=0,
        realized_pnl_today=Decimal("0"),
        realized_pnl_week=Decimal("0"),
        approval_status="approved",
        confirmation_text=build_confirmation_text(idea),
    )
    assert decision == GateDecision(
        allowed=False,
        action="dry_run_only",
        reasons=("kill_switch_enabled", "live_orders_enabled_false", "max_loss_exceeds_config"),
    )


def test_gate_allows_only_after_all_limits_and_exact_approval_are_valid():
    idea = sample_idea()
    config = GateConfig(
        live_orders_enabled=True,
        kill_switch_enabled=False,
        max_capital=Decimal("5000"),
        max_risk_per_trade=Decimal("75"),
        max_daily_loss=Decimal("100"),
        max_weekly_loss=Decimal("150"),
        max_open_positions=1,
        allowed_product_types=("CNC",),
        allowed_order_types=("LIMIT",),
    )
    decision = evaluate_gate(
        config,
        idea,
        open_positions=0,
        realized_pnl_today=Decimal("0"),
        realized_pnl_week=Decimal("0"),
        approval_status="approved",
        confirmation_text=build_confirmation_text(idea),
    )
    assert decision == GateDecision(allowed=True, action="ready_for_manual_live_execution", reasons=())


def test_gate_blocks_bad_confirmation_and_position_limit():
    idea = sample_idea()
    config = GateConfig(
        live_orders_enabled=True,
        kill_switch_enabled=False,
        max_capital=Decimal("5000"),
        max_risk_per_trade=Decimal("75"),
        max_daily_loss=Decimal("100"),
        max_weekly_loss=Decimal("150"),
        max_open_positions=1,
        allowed_product_types=("CNC",),
        allowed_order_types=("LIMIT",),
    )
    decision = evaluate_gate(
        config,
        idea,
        open_positions=1,
        realized_pnl_today=Decimal("0"),
        realized_pnl_week=Decimal("0"),
        approval_status="approved",
        confirmation_text="approve it",
    )
    assert decision.allowed is False
    assert decision.action == "dry_run_only"
    assert "max_open_positions_reached" in decision.reasons
    assert "confirmation_text_mismatch" in decision.reasons


def test_load_config_rejects_string_booleans_for_safety_flags(tmp_path: Path):
    config_path = tmp_path / "gate.json"
    # bool("false") is True — a string typo here must never enable live orders.
    config_path.write_text(json.dumps({"live_orders_enabled": "false", "kill_switch_enabled": True}), encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(config_path)


def test_idea_row_without_max_loss_amount_is_blocked_by_gate():
    row = (1, "NSE:SBIN-EQ", "BUY", "4", "LIMIT", "750.50", None, "CNC", "DAY", "735.50", "780.50", "rationale", {})
    idea = draft_from_idea_row(row)
    assert idea.max_loss_amount is None

    config = GateConfig(
        live_orders_enabled=True,
        kill_switch_enabled=False,
        max_capital=Decimal("5000"),
        max_risk_per_trade=Decimal("75"),
        max_daily_loss=Decimal("100"),
        max_weekly_loss=Decimal("150"),
        max_open_positions=1,
        allowed_product_types=("CNC",),
        allowed_order_types=("LIMIT",),
    )
    decision = evaluate_gate(
        config,
        idea,
        open_positions=0,
        realized_pnl_today=Decimal("0"),
        realized_pnl_week=Decimal("0"),
        approval_status="approved",
        confirmation_text=build_confirmation_text(idea),
    )
    assert decision.allowed is False
    assert "max_loss_exceeds_config" in decision.reasons
