#!/usr/bin/env python3
"""Run Nifty Iron Condor paper/proxy backtests."""
from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.nifty_iron_condor_strategy import backtest_nifty_iron_condor, evaluate_nifty_iron_condor
from scripts.proxy_backtest_common import by_day, connect_db, fetch_candles, summarize_results, trading_days

NIFTY_SYMBOL = "NSE:NIFTY50-INDEX"


def run_one(conn, day: date, *, verbose: bool = True) -> dict:
    rows = by_day(fetch_candles(conn, NIFTY_SYMBOL, day, day)).get(day, [])
    if not rows:
        if verbose:
            print(f"{day}: no candles")
        return {"status": "no_data", "date": day.isoformat()}
    signal = evaluate_nifty_iron_condor(rows, trade_date=day, spot=rows[-1].close)
    if signal is None:
        if verbose:
            print(f"{day}: no signal")
        return {"status": "no_signal", "date": day.isoformat()}
    result = backtest_nifty_iron_condor(signal, rows)
    if verbose:
        print(f"{day}: signal {signal.entry_time.strftime('%H:%M')} pnl ₹{result['pnl']:+,.2f} exit={result['exit_reason']}")
    return {"status": "backtest_complete", "date": day.isoformat(), "pnl": result["pnl"], "signal": signal, **result}


def run_scan(start: date, end: date) -> dict:
    results = []
    with connect_db() as conn:
        for day in trading_days(start, end):
            result = run_one(conn, day, verbose=False)
            if result.get("status") == "backtest_complete":
                results.append(result)
    summary = summarize_results(results)
    print("\n============================================================")
    print("  NIFTY IRON CONDOR MULTI-DAY SCAN")
    print(f"  {start} to {end}")
    print("============================================================\n")
    print(f"  Signals: {summary['signals']}")
    print(f"  Wins: {summary['wins']} | Losses: {summary['losses']}")
    print(f"  Total P&L: ₹{summary['total_pnl']:+,.2f}")
    for row in results:
        print(f"    {row['date']}: ₹{Decimal(row['pnl']):+,.2f} | exit: {row['exit_reason']}")
    return {"status": "scan_complete", **summary, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="Nifty Iron Condor proxy backtest")
    parser.add_argument("--mode", choices=["backtest", "scan-range", "paper"], default="backtest")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--end-date", type=date.fromisoformat, default=None)
    args = parser.parse_args()

    if args.mode == "scan-range":
        if args.end_date is None:
            raise SystemExit("--end-date required for scan-range")
        status = run_scan(args.date, args.end_date)
    else:
        with connect_db() as conn:
            status = run_one(conn, args.date, verbose=True)
    print(f"\n  Done. Mode: {args.mode} | Result: {status.get('status', '?')}")


if __name__ == "__main__":
    main()
