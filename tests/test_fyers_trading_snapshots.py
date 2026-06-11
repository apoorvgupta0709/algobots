from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import ingest_fyers_trading_snapshots as snapshots


def test_snapshot_specs_are_read_only_and_cover_phase_2_resources() -> None:
    assert set(snapshots.SNAPSHOT_SPECS) == {"positions", "orderbook", "holdings", "funds"}
    forbidden = {
        "place_order",
        "modify_order",
        "cancel_order",
        "exit_positions",
        "convert_position",
        "place_basket_orders",
        "place_multileg_order",
    }
    methods = {spec.api_method for spec in snapshots.SNAPSHOT_SPECS.values()}
    assert forbidden.isdisjoint(methods)


def test_mask_account_ref_does_not_echo_full_identifier() -> None:
    assert snapshots.mask_account_ref(None) is None
    assert snapshots.mask_account_ref("AB") == "**"
    assert snapshots.mask_account_ref("FY1234567890") == "FY********90"


def test_normalize_log_path_falls_back_when_existing_sdk_log_file_is_not_writable(tmp_path) -> None:
    bad_dir = tmp_path / "bad-logs"
    bad_dir.mkdir()
    sdk_log = bad_dir / "fyersApi.log"
    sdk_log.write_text("existing")
    sdk_log.chmod(0o400)

    assert snapshots.normalize_log_path(str(bad_dir)) == "/tmp/"


def test_collect_snapshots_calls_only_selected_read_only_methods() -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.called: list[str] = []

        def positions(self):
            self.called.append("positions")
            return {"s": "ok", "netPositions": []}

        def orderbook(self):
            self.called.append("orderbook")
            return {"s": "ok", "orderBook": []}

        def holdings(self):
            self.called.append("holdings")
            return {"s": "ok", "holdings": []}

        def funds(self):
            self.called.append("funds")
            return {"s": "ok", "fund_limit": []}

        def place_order(self, *args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("unsafe order method called")

    api = FakeApi()
    result = snapshots.collect_snapshots(api, ["positions", "funds"])

    assert api.called == ["positions", "funds"]
    assert [(item.name, item.table) for item in result] == [
        ("positions", "trading.positions_snapshots"),
        ("funds", "trading.funds_snapshots"),
    ]
    assert result[0].payload == {"netPositions": []}
    assert result[1].payload == {"fund_limit": []}
