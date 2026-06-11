from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import run_watchlist_daily_report as runner


def write_watchlist(path: Path) -> None:
    path.write_text(
        "symbol,fyers_symbol,company,sector,basket,notes\n"
        "AAA,NSE:AAA-EQ,AAA Ltd,Test,core,first\n"
        "BBB,NSE:BBB-EQ,BBB Ltd,Test,core,second\n",
        encoding="utf-8",
    )


def test_default_date_range_uses_365_day_lookback() -> None:
    range_from, range_to = runner.default_date_range(date(2026, 6, 3))

    assert range_from == "2025-06-03"
    assert range_to == "2026-06-03"


def test_run_orchestrates_history_quotes_factors_and_report(monkeypatch, tmp_path, capsys) -> None:
    watchlist = tmp_path / "watchlist.csv"
    output = tmp_path / "report.md"
    write_watchlist(watchlist)
    calls: list[tuple[str, object]] = []

    def fake_history(symbols, resolution, range_from, range_to, cont_flag):
        calls.append(("history", (symbols, resolution, range_from, range_to, cont_flag)))

    def fake_quotes(symbols):
        calls.append(("quotes", symbols))

    def fake_refresh(symbols, resolution):
        calls.append(("factors", (symbols, resolution)))
        return 2

    def fake_build_report(symbols, resolution, limit, output_path):
        calls.append(("report", (symbols, resolution, limit, output_path)))
        output_path.write_text("## Daily Market Report\nScope: read-only research report; no orders placed.\n", encoding="utf-8")
        return output_path.read_text(encoding="utf-8"), output_path

    monkeypatch.setattr(runner.history, "run_ingest", fake_history)
    monkeypatch.setattr(runner.quotes, "run_ingest", fake_quotes)
    monkeypatch.setattr(runner, "refresh_factors", fake_refresh)
    monkeypatch.setattr(runner, "build_report", fake_build_report)

    args = runner.build_parser().parse_args([
        "--watchlist", str(watchlist),
        "--from", "2026-01-01",
        "--to", "2026-06-03",
        "--output", str(output),
        "--print",
    ])

    path = runner.run(args)

    assert path == output
    assert output.exists()
    assert calls == [
        ("history", (["NSE:AAA-EQ", "NSE:BBB-EQ"], "D", "2026-01-01", "2026-06-03", "1")),
        ("quotes", ["NSE:AAA-EQ", "NSE:BBB-EQ"]),
        ("factors", (["NSE:AAA-EQ", "NSE:BBB-EQ"], "D")),
        ("report", (["NSE:AAA-EQ", "NSE:BBB-EQ"], "D", 25, output)),
    ]
    captured = capsys.readouterr().out
    assert "no orders placed" in captured.lower()
    assert "execution calls" in captured.lower()


def test_run_batches_quote_ingestion_for_large_watchlists(monkeypatch, tmp_path) -> None:
    watchlist = tmp_path / "large_watchlist.csv"
    output = tmp_path / "report.md"
    rows = ["symbol,fyers_symbol,company,sector,basket,notes\n"]
    for idx in range(101):
        rows.append(f"SYM{idx},NSE:SYM{idx}-EQ,Company {idx},Test,large,chunk test\n")
    watchlist.write_text("".join(rows), encoding="utf-8")
    quote_batches: list[list[str]] = []

    monkeypatch.setattr(runner.history, "run_ingest", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner.quotes, "run_ingest", lambda symbols: quote_batches.append(list(symbols)))
    monkeypatch.setattr(runner, "refresh_factors", lambda symbols, resolution: 101)
    monkeypatch.setattr(
        runner,
        "build_report",
        lambda symbols, resolution, limit, output_path: ("## Report\nno orders placed\n", output),
    )

    args = runner.build_parser().parse_args([
        "--watchlist", str(watchlist),
        "--output", str(output),
    ])

    assert runner.run(args) == output
    assert [len(batch) for batch in quote_batches] == [50, 50, 1]


def test_run_skip_flags_avoid_fyers_and_factor_calls(monkeypatch, tmp_path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    output = tmp_path / "report.md"
    write_watchlist(watchlist)

    def forbidden(*args, **kwargs):
        raise AssertionError("ingestion/factor call should have been skipped")

    monkeypatch.setattr(runner.history, "run_ingest", forbidden)
    monkeypatch.setattr(runner.quotes, "run_ingest", forbidden)
    monkeypatch.setattr(runner, "refresh_factors", forbidden)
    monkeypatch.setattr(
        runner,
        "build_report",
        lambda symbols, resolution, limit, output_path: ("## Report\nno orders placed\n", output),
    )

    args = runner.build_parser().parse_args([
        "--watchlist", str(watchlist),
        "--skip-history",
        "--skip-quotes",
        "--skip-factors",
        "--output", str(output),
    ])

    assert runner.run(args) == output


def test_run_rejects_empty_watchlist(tmp_path) -> None:
    watchlist = tmp_path / "empty.csv"
    watchlist.write_text("symbol,fyers_symbol,company,sector,basket,notes\nAAA,,AAA Ltd,Test,core,no fyers\n", encoding="utf-8")
    args = runner.build_parser().parse_args(["--watchlist", str(watchlist)])

    with pytest.raises(SystemExit, match="No FYERS symbols"):
        runner.run(args)


def test_run_rejects_invalid_limit(tmp_path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    write_watchlist(watchlist)
    args = runner.build_parser().parse_args(["--watchlist", str(watchlist), "--limit", "0"])

    with pytest.raises(SystemExit, match="--limit must be positive"):
        runner.run(args)


def test_run_rejects_reversed_date_range(tmp_path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    write_watchlist(watchlist)
    args = runner.build_parser().parse_args([
        "--watchlist", str(watchlist),
        "--from", "2026-06-04",
        "--to", "2026-06-03",
    ])

    with pytest.raises(SystemExit, match="--from must be on or before --to"):
        runner.run(args)


def test_run_rejects_malformed_dates(tmp_path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    write_watchlist(watchlist)
    args = runner.build_parser().parse_args([
        "--watchlist", str(watchlist),
        "--from", "06-04-2026",
    ])

    with pytest.raises(SystemExit, match="--from must be YYYY-MM-DD"):
        runner.run(args)


def test_run_rejects_non_padded_dates(tmp_path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    write_watchlist(watchlist)
    args = runner.build_parser().parse_args([
        "--watchlist", str(watchlist),
        "--from", "2026-1-01",
    ])

    with pytest.raises(SystemExit, match="--from must be YYYY-MM-DD"):
        runner.run(args)


def test_run_rejects_one_sided_reversed_date_range_after_defaults(tmp_path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    write_watchlist(watchlist)
    args = runner.build_parser().parse_args([
        "--watchlist", str(watchlist),
        "--from", "9999-01-01",
    ])

    with pytest.raises(SystemExit, match="--from must be on or before --to"):
        runner.run(args)


def test_run_rejects_unsupported_resolution(tmp_path) -> None:
    watchlist = tmp_path / "watchlist.csv"
    write_watchlist(watchlist)
    args = runner.build_parser().parse_args([
        "--watchlist", str(watchlist),
        "--resolution", "BAD",
    ])

    with pytest.raises(SystemExit, match="--resolution must be one of"):
        runner.run(args)


def test_orchestrator_has_no_order_execution_calls() -> None:
    source = Path(runner.__file__).read_text(encoding="utf-8")
    forbidden_calls = ["place_order(", "modify_order(", "cancel_order(", "exit_positions("]

    assert all(call not in source for call in forbidden_calls)
