from decimal import Decimal
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_paper_algobot import (
    evaluate_exit,
    is_pending_entry_expired,
    next_trailing_stop,
    parse_money,
    size_quantity,
    week_start_utc,
)


def test_parse_money_handles_inr_and_commas():
    assert parse_money("₹1,579.00") == Decimal("1579.00")
    assert parse_money("n/a") is None


def test_size_quantity_respects_risk_and_position_value():
    qty, risk, value = size_quantity(Decimal("102.03"), Decimal("97.26"), Decimal("50"), Decimal("2500"))
    assert qty == 10
    assert risk == Decimal("47.70")
    assert value == Decimal("1020.30")


def test_size_quantity_rejects_invalid_long_stop():
    qty, risk, value = size_quantity(Decimal("92.90"), Decimal("94.34"), Decimal("50"), Decimal("2500"))
    assert (qty, risk, value) == (0, Decimal("0"), Decimal("0"))


def test_evaluate_exit_stop_and_target():
    assert evaluate_exit(Decimal("96"), Decimal("102"), Decimal("97"), Decimal("112"), 10) == (
        "stop_loss",
        Decimal("97"),
        Decimal("-50.00"),
    )
    assert evaluate_exit(Decimal("113"), Decimal("102"), Decimal("97"), Decimal("112"), 10) == (
        "target",
        Decimal("112"),
        Decimal("100.00"),
    )
    assert evaluate_exit(Decimal("105"), Decimal("102"), Decimal("97"), Decimal("112"), 10) == (None, None, None)


def test_next_trailing_stop_activates_after_one_r_and_never_lowers():
    highest, stop, changed = next_trailing_stop(
        entry_price=Decimal("100"),
        initial_stop=Decimal("95"),
        current_stop=Decimal("95"),
        highest_price=Decimal("104"),
        ltp=Decimal("110"),
        activation_r=Decimal("1"),
        trail_r=Decimal("1"),
    )
    assert highest == Decimal("110")
    assert stop == Decimal("105")
    assert changed is True

    lower_highest, lower_stop, lower_changed = next_trailing_stop(
        entry_price=Decimal("100"),
        initial_stop=Decimal("95"),
        current_stop=stop,
        highest_price=highest,
        ltp=Decimal("106"),
        activation_r=Decimal("1"),
        trail_r=Decimal("1"),
    )
    assert lower_highest == Decimal("110")
    assert lower_stop == Decimal("105")
    assert lower_changed is False


def test_evaluate_exit_can_use_trailing_stop_and_time_stop():
    assert evaluate_exit(
        Decimal("104"),
        Decimal("100"),
        Decimal("95"),
        Decimal("120"),
        10,
        active_stop=Decimal("105"),
    ) == ("trailing_stop", Decimal("105"), Decimal("50.00"))

    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    entry_time = now - timedelta(days=4)
    assert evaluate_exit(
        Decimal("103"),
        Decimal("100"),
        Decimal("95"),
        Decimal("120"),
        10,
        now=now,
        entry_time=entry_time,
        max_holding_days=3,
    ) == ("time_stop", Decimal("103"), Decimal("30.00"))


def test_pending_entry_expiry_after_configured_days():
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    assert is_pending_entry_expired(now - timedelta(days=2, minutes=1), now, expiry_days=2) is True
    assert is_pending_entry_expired(now - timedelta(days=1, hours=23), now, expiry_days=2) is False


def test_week_start_utc_is_monday():
    assert week_start_utc(date(2026, 6, 5)) == date(2026, 6, 1)
