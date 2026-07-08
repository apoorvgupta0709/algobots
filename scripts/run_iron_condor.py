#!/usr/bin/env python3
"""BankNifty Iron Condor — backtest and paper-scan runner.

Paper-only. No FYERS order APIs. Backtests use stored 5-minute underlying
candles and proxy option pricing.

Modes:
  --mode backtest   Run on historical candles, produce P&L report
  --mode paper      Run on live market, check signal, store in DB
  --dry-run         Print signal without DB writes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.iron_condor_strategy import (
    Candle,
    IronCondorSignal,
    evaluate_bn_iron_condor,
    backtest_iron_condor,
    q2,
    D,
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")
IST = timezone(timedelta(hours=5, minutes=30))

BN_SYMBOL = "NSE:NIFTYBANK-INDEX"
LOT_SIZE = 30
STRIKE_STEP = Decimal("100")
MAX_LOSS = Decimal("18000")


def load_candles(conn, symbol: str, trade_date: date) -> list[Candle]:
    """Load 5-min candles for the given symbol and date."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts, open, high, low, close, volume
            FROM market.candles
            WHERE symbol = %s
              AND resolution = '5'
              AND ts::date = %s
            ORDER BY ts
            """,
            (symbol, trade_date),
        )
        return [
            Candle(
                ts=row[0].astimezone(IST),
                open=D(row[1]),
                high=D(row[2]),
                low=D(row[3]),
                close=D(row[4]),
                volume=int(row[5] or 0),
            )
            for row in cur
        ]


def load_option_contracts(conn, underlying: str, trade_date: date) -> list[dict]:
    """Load option contracts for the given underlying."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT strike, option_type, expiry, lot_size
            FROM research.option_contracts
            WHERE underlying = %s
              AND expiry > %s
            ORDER BY expiry, strike
            """,
            (underlying, trade_date),
        )
        return [
            {
                "strike": D(row[0]),
                "option_type": row[1],
                "expiry": row[2],
                "lot_size": row[3],
            }
            for row in cur
        ]


def get_spot(conn, symbol: str, trade_date: date) -> Decimal | None:
    """Get latest spot price for the symbol on the given date."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ltp FROM market.quotes
            WHERE symbol = %s
              AND quote_time::date = %s
              AND ltp > 0
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (symbol, trade_date),
        )
        row = cur.fetchone()
        return D(row[0]) if row else None


def get_vix(conn, trade_date: date) -> Decimal | None:
    """Get India VIX value for the given date."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ltp FROM market.quotes
            WHERE symbol = 'NSE:INDIAVIX-INDEX'
              AND quote_time::date = %s
              AND ltp > 0
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (trade_date,),
        )
        row = cur.fetchone()
        return D(row[0]) if row else Decimal("15")  # default fallback


def get_atm_iv(conn, underlying: str, spot: Decimal, trade_date: date) -> Decimal | None:
    """Estimate ATM IV from nearest ATM option chain quote."""
    if spot <= 0:
        return Decimal("15")
    # Find ATM strike
    atm_target = (spot / STRIKE_STEP).quantize(Decimal("1")) * STRIKE_STEP
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT strike FROM research.option_contracts
            WHERE underlying = %s
              AND expiry > %s
              AND option_type = 'CE'
            ORDER BY ABS(strike - %s)
            LIMIT 1
            """,
            (underlying, trade_date, float(atm_target)),
        )
        return Decimal("15")  # Proxy: return 15% as default ATM IV


def save_trade_to_db(conn, signal: IronCondorSignal, trade_date: date) -> int:
    """Store an iron condor paper trade in the strategy_pack_paper_trades table."""
    from datetime import date as dt_date

    expiry = signal.metadata.get("expiry", "")
    if isinstance(expiry, str):
        try:
            expiry_date = dt_date.fromisoformat(expiry)
        except ValueError:
            expiry_date = trade_date
    else:
        expiry_date = expiry

    raw_data = {
        "strategy": "banknifty_iron_condor",
        "entry_time": signal.entry_time.isoformat(),
        "spot": float(signal.underlying_entry),
        "sold_put": float(signal.sold_put_strike),
        "bought_put": float(signal.bought_put_strike),
        "sold_call": float(signal.sold_call_strike),
        "bought_call": float(signal.bought_call_strike),
        "net_credit": float(signal.net_credit),
        "max_loss": float(signal.max_loss_rupees),
        "stop_upper": float(signal.stop_underlying),
        "target": float(signal.target_underlying),
        "expiry": str(expiry_date),
        "metadata": signal.metadata,
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO research.strategy_pack_paper_trades
                (campaign_id, strategy_id, strategy_name, underlying, underlying_symbol,
                 direction, structure, status, signal_reason,
                 entry_time, entry_underlying, entry_proxy_premium,
                 risk_rupees, max_loss_rupees, target_r,
                 stop_underlying, target_underlying,
                 paper_only, live_orders_enabled, raw)
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s, %s
            )
            RETURNING pack_trade_id
            """,
            (
                640,  # Current campaign_id for June 25
                "banknifty_iron_condor",
                "BankNifty Iron Condor",
                "BANKNIFTY",
                BN_SYMBOL,
                signal.direction,
                signal.structure,
                "open",
                signal.reason,
                signal.entry_time,
                float(signal.underlying_entry),
                float(signal.net_credit),
                float(signal.max_loss_rupees),
                float(signal.max_loss_rupees),
                1.0,  # target_r = 1 (50% of credit is ~0.5R)
                float(signal.stop_underlying),
                float(signal.target_underlying),
                True,  # paper_only
                False,  # live_orders_enabled
                json.dumps(raw_data),
            ),
        )
        trade_id = cur.fetchone()[0]
        conn.commit()
        return trade_id


def run_backtest(conn, trade_date: date, *, show_output: bool = True) -> dict[str, Any]:
    """Run iron condor backtest on historical data."""
    print(f"\n{'='*60}")
    print(f"  IRON CONDOR BACKTEST — {trade_date}")
    print(f"{'='*60}\n")

    # Load data
    candles = load_candles(conn, BN_SYMBOL, trade_date)
    contracts = load_option_contracts(conn, "BANKNIFTY", trade_date)
    spot = get_spot(conn, BN_SYMBOL, trade_date)
    vix = get_vix(conn, trade_date)
    atm_iv = get_atm_iv(conn, "BANKNIFTY", spot or Decimal("58000"), trade_date)

    if not candles:
        print("  ❌ No candles found for this date")
        return {"status": "no_data", "reason": "no_candles"}
    if not spot or spot <= 0:
        print("  ⚠️  No spot price, using last candle close")
        spot = candles[-1].close

    print(f"  Date:        {trade_date}")
    print(f"  Candles:     {len(candles)}")
    print(f"  Spot:        ₹{spot:,.2f}")
    print(f"  VIX:         {vix}%")
    print(f"  Contracts:   {len(contracts)}")

    # Evaluate
    signal = evaluate_bn_iron_condor(
                candles,
                trade_date=trade_date,
                option_contracts=contracts,
                spot=spot,
                lot_size=LOT_SIZE,
                strike_step=STRIKE_STEP,
                max_loss_cap=MAX_LOSS,
            )

    if signal is None:
        print("\n  ❌ No iron condor signal triggered")
        return {"status": "no_signal", "spot": float(spot), "vix": float(vix)}

    if show_output is False:
        print(f"\n  ✅ Iron Condor Signal! (scan-range)")
        print(f"  Entry:       {signal.entry_time.strftime('%H:%M')} @ ₹{float(signal.underlying_entry):,.2f}")
    else:
        # Signal details
        print(f"\n  ✅ Iron Condor Signal!")
        print(f"  Entry:       {signal.entry_time.strftime('%H:%M')} @ ₹{float(signal.underlying_entry):,.2f}")
        print(f"  Put spread:  Sell {float(signal.sold_put_strike):,.0f}P / Buy {float(signal.bought_put_strike):,.0f}P")
        print(f"  Call spread: Sell {float(signal.sold_call_strike):,.0f}C / Buy {float(signal.bought_call_strike):,.0f}C")
        print(f"  Net credit:  ₹{float(signal.net_credit):,.2f}")
        print(f"  Max loss:    ₹{float(signal.max_loss_rupees):,.2f}")
        print(f"  Stop:        ₹{float(signal.stop_underlying):,.2f}")
        print(f"  Expiry:      {signal.metadata.get('expiry', '?')}")

    # Always run backtest
    bt_result = backtest_iron_condor(signal, candles, lot_size=LOT_SIZE)
    if show_output:
        print(f"\n  📊 Backtest P&L:")
    print(f"     Exit:     {bt_result['exit_time'].strftime('%H:%M') if hasattr(bt_result['exit_time'], 'strftime') else bt_result['exit_time']} @ ₹{float(bt_result['exit_underlying']):,.2f}")
    print(f"     Reason:   {bt_result['exit_reason']}")
    print(f"     P&L:      {'🟢' if float(bt_result['realized_pnl']) >= 0 else '🔴'} ₹{float(bt_result['realized_pnl']):+,.2f}")

    bt_result["signal"] = {
        "entry_time": signal.entry_time.isoformat(),
        "entry_spot": float(signal.underlying_entry),
        "sold_put": float(signal.sold_put_strike),
        "bought_put": float(signal.bought_put_strike),
        "sold_call": float(signal.sold_call_strike),
        "bought_call": float(signal.bought_call_strike),
        "net_credit": float(signal.net_credit),
        "max_loss": float(signal.max_loss_rupees),
    }
    # Save trade to DB (only for paper mode; backtest/scan-range don't write)
    return {"status": "backtest_complete", **bt_result}


def run_mode_backtest(args):
    """Backtest mode: run on historical data."""
    with psycopg.connect(DATABASE_URL) as conn:
        result = run_backtest(conn, args.date)
        return result


def run_mode_paper(args):
    """Paper mode: evaluate live market, store trade if signal fires."""
    today = args.date or date.today()

    with psycopg.connect(DATABASE_URL) as conn:
        candles = load_candles(conn, BN_SYMBOL, today)
        contracts = load_option_contracts(conn, "BANKNIFTY", today)
        spot = get_spot(conn, BN_SYMBOL, today) or candles[-1].close if candles else Decimal("0")
        vix = get_vix(conn, today)
        atm_iv = get_atm_iv(conn, "BANKNIFTY", spot, today)

        if not candles:
            print("❌ No candles available for today")
            return {"status": "no_data"}

        print(f"\n{'='*60}")
        print(f"  IRON CONDOR PAPER SCAN — {today}")
        print(f"{'='*60}\n")

        # Check if we already have an open iron condor trade
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pack_trade_id FROM research.strategy_pack_paper_trades
                WHERE strategy_id = 'banknifty_iron_condor'
                  AND status = 'open'
                  AND entry_time::date = %s
                LIMIT 1
                """,
                (today,),
            )
            existing = cur.fetchone()

        if existing:
            print(f"  ℹ️  Existing open iron condor trade ID: {existing[0]}")
            return {"status": "already_open", "trade_id": existing[0]}

        signal = evaluate_bn_iron_condor(
            candles,
            trade_date=today,
            option_contracts=contracts,
            spot=spot,
            lot_size=LOT_SIZE,
            strike_step=STRIKE_STEP,
            max_loss_cap=MAX_LOSS,
        )

        if signal is None:
            print("  No iron condor signal triggered")
            return {"status": "no_signal"}

        if args.dry_run:
            print("\n  💠 DRY RUN — signal found but not stored")
            print(f"  Entry: {signal.entry_time.strftime('%H:%M')} @ ₹{float(signal.underlying_entry):,.2f}")
            print(f"  Net credit: ₹{float(signal.net_credit):,.2f}")
            return {"status": "dry_run", "signal": str(signal)}

        # Store the trade
        trade_id = save_trade_to_db(conn, signal, today)
        print(f"\n  ✅ Iron condor trade stored! ID: {trade_id}")
        print(f"  Entry: {signal.entry_time.strftime('%H:%M')} @ ₹{float(signal.underlying_entry):,.2f}")

        return {"status": "trade_stored", "trade_id": trade_id}


def run_mode_scan_range(args):
    """Scan a range of dates for iron condor signals (multi-backtest)."""
    if not args.end_date:
        print("❌ --end-date required for scan-range mode")
        return {"status": "error", "reason": "missing_end_date"}

    current = args.date
    end = args.end_date
    results = []

    print(f"\n{'='*60}")
    print(f"  IRON CONDOR MULTI-DAY SCAN")
    print(f"  {current} to {end}")
    print(f"{'='*60}\n")

    with psycopg.connect(DATABASE_URL) as conn:
        day = current
        day_count = 0
        signal_count = 0

        while day <= end:
            # Skip weekends
            if day.weekday() >= 5:
                day += timedelta(days=1)
                continue

            result = run_backtest(conn, day, show_output=False)
            day_count += 1

            if result.get("status") == "signal_found" or result.get("status") == "backtest_complete":
                signal_count += 1
                pnl = result.get("realized_pnl", 0)
                if isinstance(pnl, Decimal):
                    pnl = float(pnl)
                signal_data = result.get("signal", {})
                if isinstance(signal_data, dict):
                    net_credit = signal_data.get("net_credit", 0)
                    entry_spot = signal_data.get("entry_spot", 0)
                else:
                    net_credit = 0
                    entry_spot = 0
                results.append({
                    "date": day.isoformat(),
                    "status": result["status"],
                    "pnl": pnl,
                    "net_credit": net_credit,
                    "entry_spot": entry_spot,
                    "exit_reason": result.get("exit_reason", ""),
                })

            day += timedelta(days=1)

    print(f"\n  Scanned: {day_count} days")
    print(f"  Signals: {signal_count}")
    if results:
        wins = sum(1 for r in results if r.get("pnl", 0) > 0)
        losses = sum(1 for r in results if r.get("pnl", 0) < 0)
        total_pnl = sum(r.get("pnl", 0) for r in results)
        print(f"  Wins: {wins} | Losses: {losses}")
        print(f"  Total P&L: ₹{total_pnl:+,.2f}")
        print(f"\n  Details:")
        for r in results:
            pnl = r.get("pnl", 0)
            icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
            print(f"    {icon} {r['date']}: ₹{pnl:+,.2f} | credit: ₹{r.get('net_credit',0):.0f} | exit: {r.get('exit_reason','?')}")

    return {"status": "scan_complete", "days_scanned": day_count, "signals": signal_count, "results": results}


def main():
    parser = argparse.ArgumentParser(description="BankNifty Iron Condor")
    parser.add_argument("--mode", choices=["backtest", "paper", "scan-range"], default="backtest",
                        help="backtest on historical data, paper-scan live, or scan date range")
    parser.add_argument("--date", type=date.fromisoformat, default=None,
                        help="Trading date (YYYY-MM-DD). Default: today")
    parser.add_argument("--end-date", type=date.fromisoformat, default=None,
                        help="End date for scan-range mode (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print signal without DB writes")

    args = parser.parse_args()
    if args.date is None:
        args.date = date.today()

    if args.mode == "backtest":
        result = run_mode_backtest(args)
    elif args.mode == "paper":
        result = run_mode_paper(args)
    elif args.mode == "scan-range":
        result = run_mode_scan_range(args)
    else:
        print(f"Unknown mode: {args.mode}")
        return

    print(f"\n  Done. Mode: {args.mode} | Result: {result.get('status', '?')}")


if __name__ == "__main__":
    main()