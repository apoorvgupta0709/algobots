#!/usr/bin/env python3
"""Backtest and paper-scan the NSE intraday options strategy pack.

Paper/proxy only. No FYERS order APIs are imported or called. Live orders are
blocked by config and DB constraints. Backtests use stored 5-minute underlying
candles and simulate option/spread P&L with transparent proxy risk rules because
expired option-chain candles are not available for the full history.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.nse_intraday_options_strategy_pack import (  # noqa: E402
    Candle,
    StrategyPackConfig,
    StrategySignal,
    build_default_config,
    config_to_json_dict,
    evaluate_cpr_trend_debit_spread,
    evaluate_expiry_tuesday_directional,
    evaluate_nifty_orb_debit_spread,
    evaluate_nifty_vwap_mean_reversion,
    evaluate_single_stock_momentum,
    load_config,
    save_default_config,
)

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "nse_intraday_options_strategy_pack.json"
REPORT_DIR = PROJECT_ROOT / "reports"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")
IST = timezone(timedelta(hours=5, minutes=30))
TWO = Decimal("0.01")

SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "HDFCBANK": "NSE:HDFCBANK-EQ",
    "ICICIBANK": "NSE:ICICIBANK-EQ",
    "SBIN": "NSE:SBIN-EQ",
    "RELIANCE": "NSE:RELIANCE-EQ",
    "INFY": "NSE:INFY-EQ",
    "TCS": "NSE:TCS-EQ",
}
BANK_STOCKS = {"HDFCBANK", "ICICIBANK", "SBIN"}
STOCK_LOTS = {"RELIANCE": 500, "HDFCBANK": 550, "ICICIBANK": 700, "SBIN": 750, "INFY": 400, "TCS": 175}
STOCK_DEBIT_CAP = {"RELIANCE": Decimal("3.0"), "HDFCBANK": Decimal("2.7"), "ICICIBANK": Decimal("2.1"), "SBIN": Decimal("2.0"), "INFY": Decimal("3.75"), "TCS": Decimal("8.6")}

@dataclass
class ProxyTrade:
    strategy_id: str
    strategy_name: str
    day: date
    underlying: str
    underlying_symbol: str
    direction: str
    structure: str
    entry_time: datetime
    exit_time: datetime
    entry_underlying: Decimal
    exit_underlying: Decimal
    risk_rupees: Decimal
    pnl_rupees: Decimal
    pnl_r: Decimal
    exit_reason: str
    signal_reason: str
    max_loss_rupees: Decimal
    target_r: Decimal


def money(v: Decimal) -> str:
    return f"₹{q2(v):,.2f}"


def q2(v: Decimal) -> Decimal:
    return v.quantize(TWO, rounding=ROUND_HALF_UP)


def connect_db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_config(path: Path = DEFAULT_CONFIG) -> StrategyPackConfig:
    if not path.exists():
        save_default_config(path)
    return load_config(path)


def fetch_candles(conn: psycopg.Connection, symbols: list[str], start: date, end: date, resolution: str = "5") -> dict[str, list[Candle]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select symbol, ts, open, high, low, close, volume
            from market.candles
            where symbol = any(%s)
              and resolution = %s
              and ts::date between %s and %s
            order by symbol, ts
            """,
            (symbols, resolution, start, end),
        )
        out: dict[str, list[Candle]] = defaultdict(list)
        for sym, ts, o, h, l, c, vol in cur.fetchall():
            local_ts = ts.astimezone(IST).replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts
            out[sym].append(Candle(local_ts, Decimal(str(o)), Decimal(str(h)), Decimal(str(l)), Decimal(str(c)), int(vol or 0)))
        return dict(out)


def by_day(candles: Iterable[Candle]) -> dict[date, list[Candle]]:
    days: dict[date, list[Candle]] = defaultdict(list)
    for row in candles:
        days[row.ts.date()].append(row)
    return {d: sorted(rows, key=lambda x: x.ts) for d, rows in days.items()}


def first_candle_at_or_after(rows: list[Candle], t: time) -> Candle | None:
    return next((c for c in rows if c.ts.time() >= t), None)


def intraday_pct(rows: list[Candle], until: datetime | None = None) -> Decimal:
    filtered = [c for c in rows if until is None or c.ts <= until]
    if len(filtered) < 2 or filtered[0].open == 0:
        return Decimal("0")
    return ((filtered[-1].close - filtered[0].open) / filtered[0].open * Decimal("100")).quantize(Decimal("0.0001"))


def simple_rsi9(rows: list[Candle]) -> Decimal:
    if len(rows) < 3:
        return Decimal("50")
    tail = rows[-10:]
    gains = []
    losses = []
    for a, b in zip(tail, tail[1:]):
        diff = b.close - a.close
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    avg_gain = sum(gains, Decimal("0")) / Decimal(max(1, len(gains)))
    avg_loss = sum(losses, Decimal("0")) / Decimal(max(1, len(losses)))
    if avg_loss == 0:
        return Decimal("70") if avg_gain > 0 else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def previous_trading_day(days: list[date], day: date) -> date | None:
    before = [d for d in days if d < day]
    return before[-1] if before else None


def is_cpr_narrow(prev_rows: list[Candle], underlying: str) -> bool:
    if not prev_rows:
        return False
    high = max(c.high for c in prev_rows)
    low = min(c.low for c in prev_rows)
    close = prev_rows[-1].close
    pivot = (high + low + close) / Decimal("3")
    bc = (high + low) / Decimal("2")
    tc = Decimal("2") * pivot - bc
    threshold = Decimal("0.0035") if underlying == "BANKNIFTY" else Decimal("0.003")
    return abs(tc - bc) / close <= threshold if close else False


def sessions_to_monthly_expiry(day: date, trading_days: list[date]) -> int:
    # Last Tuesday of the calendar month.
    last = date(day.year, day.month, 28)
    while (last + timedelta(days=1)).month == day.month:
        last += timedelta(days=1)
    while last.weekday() != 1:
        last -= timedelta(days=1)
    return len([d for d in trading_days if day <= d <= last])


def simulate_proxy_trade(
    signal: StrategySignal,
    rows: list[Candle],
    *,
    strategy_name: str,
    underlying: str,
    underlying_symbol: str,
    stop_pct: Decimal,
    target_r: Decimal,
    time_exit: time,
    cost_rupees: Decimal,
) -> ProxyTrade | None:
    after = [c for c in rows if c.ts > signal.entry_time]
    if not after:
        return None
    risk = max(signal.max_loss_rupees, Decimal("1"))
    sign = Decimal("1") if signal.direction in {"long", "long_ce"} else Decimal("-1")
    entry = signal.underlying_entry
    stop_underlying = entry * (Decimal("1") - sign * stop_pct)
    target_underlying = entry * (Decimal("1") + sign * stop_pct * target_r)
    exit_row = after[-1]
    pnl_r = Decimal("0")
    reason = "time_exit"
    for row in after:
        if row.ts.time() > time_exit:
            exit_row = row
            move = sign * (row.close - entry) / (entry * stop_pct)
            pnl_r = max(Decimal("-1"), min(target_r, move))
            reason = "time_exit"
            break
        # Conservative stop-first ordering if both could trigger inside one candle.
        stopped = row.low <= stop_underlying if sign > 0 else row.high >= stop_underlying
        targeted = row.high >= target_underlying if sign > 0 else row.low <= target_underlying
        if stopped:
            exit_row = row
            pnl_r = Decimal("-1")
            reason = "structure_stop"
            break
        if targeted:
            exit_row = row
            pnl_r = target_r
            reason = "target_r"
            break
    else:
        row = after[-1]
        exit_row = row
        move = sign * (row.close - entry) / (entry * stop_pct)
        pnl_r = max(Decimal("-1"), min(target_r, move))
        reason = "eod_proxy_exit"
    pnl = q2((pnl_r * risk) - cost_rupees)
    return ProxyTrade(
        strategy_id=signal.strategy_id,
        strategy_name=strategy_name,
        day=signal.entry_time.date(),
        underlying=underlying,
        underlying_symbol=underlying_symbol,
        direction=signal.direction,
        structure=signal.structure,
        entry_time=signal.entry_time,
        exit_time=exit_row.ts,
        entry_underlying=q2(entry),
        exit_underlying=q2(exit_row.close),
        risk_rupees=q2(risk),
        pnl_rupees=pnl,
        pnl_r=q2(pnl / risk),
        exit_reason=reason,
        signal_reason=signal.reason,
        max_loss_rupees=q2(signal.max_loss_rupees),
        target_r=target_r,
    )


def evaluate_day(day: date, data_by_symbol_day: dict[str, dict[date, list[Candle]]], all_days: list[date], cfg: StrategyPackConfig) -> list[tuple[StrategySignal, str, str, str, Decimal, Decimal, time]]:
    results: list[tuple[StrategySignal, str, str, str, Decimal, Decimal, time]] = []
    nifty = data_by_symbol_day.get(SYMBOLS["NIFTY"], {}).get(day, [])
    banknifty = data_by_symbol_day.get(SYMBOLS["BANKNIFTY"], {}).get(day, [])
    prev_day = previous_trading_day(all_days, day)
    prev_nifty = data_by_symbol_day.get(SYMBOLS["NIFTY"], {}).get(prev_day, []) if prev_day else []
    prev_bank = data_by_symbol_day.get(SYMBOLS["BANKNIFTY"], {}).get(prev_day, []) if prev_day else []

    if cfg.strategies["nifty_orb_debit_spread"].paper_trade_enabled and nifty:
        sig = evaluate_nifty_orb_debit_spread(nifty, vix=Decimal("15"), net_debit_per_share=Decimal("22"), lot_size=65)
        if sig:
            results.append((sig, "NIFTY", SYMBOLS["NIFTY"], "Nifty ORB Debit Spread", Decimal("0.0025"), Decimal("2"), time(13, 45)))

    if cfg.strategies["cpr_trend_debit_spread"].paper_trade_enabled:
        sig = None
        under = "NIFTY"
        symbol = SYMBOLS["NIFTY"]
        rows = nifty
        if nifty and prev_nifty:
            sig = evaluate_cpr_trend_debit_spread(nifty, previous_day=prev_nifty, underlying="NIFTY", vix=Decimal("16"), net_debit_per_share=Decimal("22"), lot_size=65, sessions_to_expiry=10)
        if not sig and banknifty and prev_bank:
            under = "BANKNIFTY"
            symbol = SYMBOLS["BANKNIFTY"]
            rows = banknifty
            sig = evaluate_cpr_trend_debit_spread(banknifty, previous_day=prev_bank, underlying="BANKNIFTY", vix=Decimal("16"), net_debit_per_share=Decimal("45"), lot_size=30, sessions_to_expiry=sessions_to_monthly_expiry(day, all_days))
        if sig:
            results.append((sig, under, symbol, "CPR Trend-Day Debit Spread", Decimal("0.0025"), Decimal("2"), time(14, 45)))

    if cfg.strategies["expiry_tuesday_directional"].paper_trade_enabled and nifty:
        sig = evaluate_expiry_tuesday_directional(nifty, trade_date=day, vix=Decimal("18"), option_premium=Decimal("80"), lot_size=65)
        if sig:
            results.append((sig, "NIFTY", SYMBOLS["NIFTY"], "Expiry Tuesday Nifty Defined-Risk Directional", Decimal("0.0020"), Decimal("1"), time(13, 0)))

    if cfg.strategies["nifty_vwap_mean_reversion"].paper_trade_enabled and nifty:
        cpr_narrow = is_cpr_narrow(prev_nifty, "NIFTY") if prev_nifty else False
        # Evaluate at each candle after 09:50 until first signal.
        for i in range(3, len(nifty)):
            partial = nifty[: i + 1]
            sig = evaluate_nifty_vwap_mean_reversion(partial, is_range_day=not cpr_narrow, is_cpr_narrow=cpr_narrow, vix=Decimal("15"), rsi9=simple_rsi9(partial), option_premium=Decimal("90"), lot_size=65)
            if sig:
                results.append((sig, "NIFTY", SYMBOLS["NIFTY"], "Nifty VWAP Mean Reversion Long", Decimal("0.0030"), Decimal("1.2"), time(14, 45)))
                break

    if cfg.strategies["single_stock_momentum_index_confirm"].paper_trade_enabled:
        for stock in ["HDFCBANK", "ICICIBANK", "SBIN", "RELIANCE", "INFY", "TCS"]:
            srows = data_by_symbol_day.get(SYMBOLS[stock], {}).get(day, [])
            idx_name = "BANKNIFTY" if stock in BANK_STOCKS else "NIFTY"
            irows = banknifty if idx_name == "BANKNIFTY" else nifty
            if not srows or not irows:
                continue
            first_break = first_candle_at_or_after(srows, time(9, 45))
            pct_until = first_break.ts if first_break else None
            sig = evaluate_single_stock_momentum(
                srows,
                irows,
                stock_symbol=stock,
                confirming_index=idx_name,
                vix=Decimal("16"),
                option_spread_pct=Decimal("0.003"),
                net_debit_per_share=STOCK_DEBIT_CAP[stock] * Decimal("0.95"),
                lot_size=STOCK_LOTS[stock],
                earnings_today=False,
                stock_intraday_pct=intraday_pct(srows, pct_until),
                index_intraday_pct=intraday_pct(irows, pct_until),
            )
            if sig:
                results.append((sig, stock, SYMBOLS[stock], "Single-Stock Momentum with Index Confirmation", Decimal("0.0040"), Decimal("2"), time(14, 30)))
                break
    return results


def run_backtest(config_path: Path, start: date, end: date) -> tuple[Path, Path, list[ProxyTrade]]:
    cfg = ensure_config(config_path)
    cfg.validate()
    symbols = list(SYMBOLS.values())
    with connect_db() as conn:
        data = fetch_candles(conn, symbols, start, end, "5")
    data_by_symbol_day = {sym: by_day(rows) for sym, rows in data.items()}
    all_days = sorted(set().union(*(set(days.keys()) for days in data_by_symbol_day.values())))
    trades: list[ProxyTrade] = []
    for day in all_days:
        if not (start <= day <= end):
            continue
        for sig, underlying, underlying_symbol, name, stop_pct, target_r, exit_t in evaluate_day(day, data_by_symbol_day, all_days, cfg):
            rows = data_by_symbol_day.get(underlying_symbol, {}).get(day, [])
            cost = Decimal("250") if sig.strategy_id == "single_stock_momentum_index_confirm" else Decimal("120")
            trade = simulate_proxy_trade(sig, rows, strategy_name=name, underlying=underlying, underlying_symbol=underlying_symbol, stop_pct=stop_pct, target_r=target_r, time_exit=exit_t, cost_rupees=cost)
            if trade:
                trades.append(trade)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = REPORT_DIR / f"nse_intraday_options_strategy_pack_proxy_trades_{stamp}.csv"
    md_path = REPORT_DIR / f"nse_intraday_options_strategy_pack_proxy_backtest_{stamp}.md"
    write_trades_csv(csv_path, trades)
    write_report(md_path, trades, start, end)
    return md_path, csv_path, trades


def write_trades_csv(path: Path, trades: list[ProxyTrade]) -> None:
    fields = list(asdict(trades[0]).keys()) if trades else ["strategy_id", "day", "pnl_rupees"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            row = {k: (str(v) if not isinstance(v, (int, float)) else v) for k, v in asdict(t).items()}
            w.writerow(row)


def summarize(trades: list[ProxyTrade]) -> dict[str, dict[str, Decimal | int]]:
    out: dict[str, dict[str, Decimal | int]] = {}
    for sid in sorted({t.strategy_id for t in trades}):
        rows = [t for t in trades if t.strategy_id == sid]
        pnl = sum((t.pnl_rupees for t in rows), Decimal("0"))
        wins = sum(1 for t in rows if t.pnl_rupees > 0)
        losses = sum(1 for t in rows if t.pnl_rupees <= 0)
        gross_profit = sum((t.pnl_rupees for t in rows if t.pnl_rupees > 0), Decimal("0"))
        gross_loss = -sum((t.pnl_rupees for t in rows if t.pnl_rupees < 0), Decimal("0"))
        pf = gross_profit / gross_loss if gross_loss else Decimal("999") if gross_profit else Decimal("0")
        avg_r = sum((t.pnl_r for t in rows), Decimal("0")) / Decimal(len(rows)) if rows else Decimal("0")
        out[sid] = {"trades": len(rows), "wins": wins, "losses": losses, "pnl": q2(pnl), "pf": q2(pf), "avg_r": q2(avg_r)}
    return out


def write_report(path: Path, trades: list[ProxyTrade], start: date, end: date) -> None:
    lines = [
        "# NSE Intraday Options Strategy Pack — Proxy Backtest",
        "",
        f"Window: {start} to {end}",
        "Mode: paper/proxy only; no live orders; option/spread legs are simulated from underlying 5-minute candles.",
        "Cost model: ₹120 index strategy round-trip, ₹250 stock-option strategy round-trip.",
        "",
        "## Summary by strategy",
    ]
    total_pnl = sum((t.pnl_rupees for t in trades), Decimal("0"))
    for sid, stats in summarize(trades).items():
        lines += [
            f"- {sid}",
            f"  - Trades: {stats['trades']}",
            f"  - Wins/Losses: {stats['wins']}/{stats['losses']}",
            f"  - P&L: {money(stats['pnl'])}",
            f"  - Profit factor: {stats['pf']}",
            f"  - Avg R: {stats['avg_r']}",
        ]
    lines += ["", f"Total trades: {len(trades)}", f"Total proxy P&L: {money(q2(total_pnl))}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def upsert_campaign(conn: psycopg.Connection, cfg: StrategyPackConfig, config_path: Path) -> int:
    raw = config_to_json_dict(cfg)
    name = f"nse_intraday_options_pack_5x50000_{date.today().isoformat()}"
    end_date = date.today() + timedelta(days=31)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into research.strategy_pack_campaigns(name, start_date, end_date, paper_only, live_orders_enabled, active, config_sha256, notes, raw)
            values (%s, current_date, %s, true, false, true, %s, %s, %s::jsonb)
            on conflict(name) do update set active=true, end_date=excluded.end_date, config_sha256=excluded.config_sha256, raw=excluded.raw, updated_at=now()
            returning campaign_id
            """,
            (name, end_date, sha256_file(config_path), cfg.notes, json.dumps(raw)),
        )
        campaign_id = int(cur.fetchone()[0])
        for sid, strat in cfg.strategies.items():
            cur.execute(
                """
                insert into research.strategy_pack_allocations(campaign_id, strategy_id, strategy_name, paper_capital, max_trade_loss, max_daily_loss, max_premium_exposure, enabled, paper_trade_enabled, raw)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                on conflict(campaign_id, strategy_id) do update set
                  paper_capital=excluded.paper_capital,
                  max_trade_loss=excluded.max_trade_loss,
                  max_daily_loss=excluded.max_daily_loss,
                  max_premium_exposure=excluded.max_premium_exposure,
                  enabled=excluded.enabled,
                  paper_trade_enabled=excluded.paper_trade_enabled,
                  raw=excluded.raw,
                  updated_at=now()
                """,
                (campaign_id, sid, strat.name, strat.paper_capital, strat.max_trade_loss, strat.max_daily_loss, strat.max_premium_exposure, strat.enabled, strat.paper_trade_enabled, json.dumps(asdict(strat), default=str)),
            )
    return campaign_id


def refresh_today_history(symbols: list[str]) -> None:
    today = date.today().isoformat()
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / "ingest_fyers_history.py"), "--resolution", "5", "--from", today, "--to", today, "--symbols", *symbols]
    env = os.environ.copy()
    env.setdefault("FYERS_LOG_PATH", "/tmp/")
    subprocess.run(cmd, cwd=PROJECT_ROOT, env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def today_scan(config_path: Path, *, refresh: bool = False) -> list[tuple[StrategySignal, str, str, str, Decimal, Decimal, time]]:
    cfg = ensure_config(config_path)
    cfg.validate()
    symbols = list(SYMBOLS.values())
    if refresh:
        refresh_today_history(symbols)
    today = date.today()
    start = today - timedelta(days=7)
    with connect_db() as conn:
        data = fetch_candles(conn, symbols, start, today, "5")
    data_by_symbol_day = {sym: by_day(rows) for sym, rows in data.items()}
    all_days = sorted(set().union(*(set(days.keys()) for days in data_by_symbol_day.values())))
    return evaluate_day(today, data_by_symbol_day, all_days, cfg)



def current_candle_for_symbol(conn: psycopg.Connection, symbol: str) -> Candle | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select ts, open, high, low, close, volume
            from market.candles
            where symbol=%s and resolution='5' and ts::date=current_date
            order by ts desc
            limit 1
            """,
            (symbol,),
        )
        row = cur.fetchone()
    if not row:
        return None
    ts, o, h, l, c, vol = row
    local_ts = ts.astimezone(IST).replace(tzinfo=None) if getattr(ts, "tzinfo", None) else ts
    return Candle(local_ts, Decimal(str(o)), Decimal(str(h)), Decimal(str(l)), Decimal(str(c)), int(vol or 0))


def close_open_proxy_trades(conn: psycopg.Connection, campaign_id: int) -> list[dict[str, Any]]:
    closed: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            select pack_trade_id, strategy_id, underlying_symbol, direction, entry_underlying, risk_rupees,
                   stop_underlying, target_underlying, target_r, raw
            from research.strategy_pack_paper_trades
            where campaign_id=%s and status='open'
            order by entry_time
            """,
            (campaign_id,),
        )
        rows = cur.fetchall()
    for trade_id, sid, symbol, direction, entry, risk, stop_underlying, target_underlying, target_r, raw in rows:
        candle = current_candle_for_symbol(conn, symbol)
        if candle is None:
            continue
        sign = Decimal("1") if direction in {"long", "long_ce"} else Decimal("-1")
        stopped = candle.low <= Decimal(str(stop_underlying)) if sign > 0 else candle.high >= Decimal(str(stop_underlying))
        targeted = candle.high >= Decimal(str(target_underlying)) if sign > 0 else candle.low <= Decimal(str(target_underlying))
        exit_reason = None
        pnl_r = Decimal("0")
        now_t = datetime.now(IST).time()
        exit_t = time.fromisoformat((raw or {}).get("time_exit", "15:20")) if isinstance(raw, dict) else time(15, 20)
        if stopped:
            exit_reason = "structure_stop"
            pnl_r = Decimal("-1")
        elif targeted:
            exit_reason = "target_r"
            pnl_r = Decimal(str(target_r))
        elif now_t >= exit_t or now_t >= time(15, 20):
            move = sign * (candle.close - Decimal(str(entry))) / abs(Decimal(str(entry)) - Decimal(str(stop_underlying)))
            pnl_r = max(Decimal("-1"), min(Decimal(str(target_r)), move))
            exit_reason = "time_exit"
        if not exit_reason:
            continue
        pnl = q2(Decimal(str(risk)) * pnl_r)
        with conn.cursor() as cur:
            cur.execute(
                """
                update research.strategy_pack_paper_trades
                set status='closed', exit_time=now(), exit_underlying=%s, realized_pnl=%s,
                    exit_reason=%s, updated_at=now()
                where pack_trade_id=%s and status='open'
                """,
                (candle.close, pnl, exit_reason, trade_id),
            )
            cur.execute(
                """
                insert into research.strategy_pack_paper_trade_events(pack_trade_id, event_type, message, raw)
                values (%s, 'closed', %s, %s::jsonb)
                """,
                (trade_id, f"Closed {sid} by {exit_reason}; proxy_pnl={pnl}", json.dumps({"candle": asdict(candle), "pnl_r": str(pnl_r)}, default=str)),
            )
        closed.append({"event": "closed", "trade_id": trade_id, "strategy_id": sid, "exit_reason": exit_reason, "pnl": str(pnl)})
    return closed


def stop_target_from_signal(sig: StrategySignal, stop_pct: Decimal, target_r: Decimal) -> tuple[Decimal, Decimal]:
    sign = Decimal("1") if sig.direction in {"long", "long_ce"} else Decimal("-1")
    stop = sig.underlying_entry * (Decimal("1") - sign * stop_pct)
    target = sig.underlying_entry * (Decimal("1") + sign * stop_pct * target_r)
    return q2(stop), q2(target)


def run_tick(config_path: Path, *, dry_run: bool = False, refresh: bool = False) -> list[dict[str, Any]]:
    cfg = ensure_config(config_path)
    cfg.validate()
    out: list[dict[str, Any]] = []
    if refresh:
        refresh_today_history(list(SYMBOLS.values()))
    with connect_db() as conn:
        campaign_id = upsert_campaign(conn, cfg, config_path)
        if not dry_run:
            out.extend(close_open_proxy_trades(conn, campaign_id))
        signals = today_scan(config_path, refresh=False)
        for sig, underlying, underlying_symbol, name, stop_pct, target_r, exit_t in signals:
            stop_u, target_u = stop_target_from_signal(sig, stop_pct, target_r)
            payload = {
                "event": "signal",
                "strategy_id": sig.strategy_id,
                "strategy_name": name,
                "underlying": underlying,
                "direction": sig.direction,
                "structure": sig.structure,
                "entry_time": sig.entry_time.isoformat(),
                "entry_underlying": str(sig.underlying_entry),
                "stop_underlying": str(stop_u),
                "target_underlying": str(target_u),
                "reason": sig.reason,
                "dry_run": dry_run,
            }
            if dry_run:
                out.append(payload)
                continue
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                      count(*) filter (where entry_time::date=current_date and status in ('open','closed')) as trades_today,
                      count(*) filter (where status='open') as open_now
                    from research.strategy_pack_paper_trades
                    where campaign_id=%s and strategy_id=%s
                    """,
                    (campaign_id, sig.strategy_id),
                )
                trades_today, open_now = cur.fetchone()
                if int(open_now or 0) >= cfg.strategies[sig.strategy_id].max_open_positions:
                    continue
                if int(trades_today or 0) >= cfg.strategies[sig.strategy_id].max_trades_per_day:
                    continue
                raw = dict(sig.metadata)
                raw.update({"stop_pct": str(stop_pct), "target_r": str(target_r), "time_exit": exit_t.isoformat(), "proxy_paper": True})
                cur.execute(
                    """
                    insert into research.strategy_pack_paper_trades(
                      campaign_id, strategy_id, strategy_name, underlying, underlying_symbol, direction, structure, status,
                      signal_reason, entry_time, entry_underlying, risk_rupees, max_loss_rupees, target_r,
                      stop_underlying, target_underlying, paper_only, live_orders_enabled, raw
                    ) values (%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,%s,%s,%s,%s,%s,true,false,%s::jsonb)
                    returning pack_trade_id
                    """,
                    (campaign_id, sig.strategy_id, name, underlying, underlying_symbol, sig.direction, sig.structure, sig.reason, sig.entry_time, sig.underlying_entry, sig.max_loss_rupees, sig.max_loss_rupees, target_r, stop_u, target_u, json.dumps(raw, default=str)),
                )
                trade_id = cur.fetchone()[0]
                cur.execute(
                    """
                    insert into research.strategy_pack_paper_trade_events(pack_trade_id, event_type, message, raw)
                    values (%s, 'opened', %s, %s::jsonb)
                    """,
                    (trade_id, f"Opened {sig.strategy_id} proxy paper trade", json.dumps(raw, default=str)),
                )
                payload["event"] = "opened"
                payload["trade_id"] = trade_id
                out.append(payload)
    return out

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=["init-config", "init-campaign", "backtest", "scan", "tick"], default="scan")
    parser.add_argument("--from", dest="start", default="2026-02-01")
    parser.add_argument("--to", dest="end", default=date.today().isoformat())
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.mode == "init-config":
        save_default_config(args.config)
        print(f"WROTE {args.config}")
        return
    cfg = ensure_config(args.config)
    if args.mode == "init-campaign":
        with connect_db() as conn:
            campaign_id = upsert_campaign(conn, cfg, args.config)
        print(f"UPSERTED campaign_id={campaign_id}; paper_only={cfg.paper_only}; live_orders_enabled={cfg.live_orders_enabled}")
        return
    if args.mode == "backtest":
        md, csv_path, trades = run_backtest(args.config, date.fromisoformat(args.start), date.fromisoformat(args.end))
        print(f"WROTE {md}")
        print(f"WROTE {csv_path}")
        print(json.dumps({sid: {k: str(v) for k, v in stats.items()} for sid, stats in summarize(trades).items()}, indent=2))
        return
    if args.mode == "scan":
        signals = today_scan(args.config, refresh=args.refresh)
        if not signals:
            print("NO_SIGNAL paper-only strategy pack scan")
        for sig, underlying, _symbol, name, *_ in signals:
            print(json.dumps({"strategy_id": sig.strategy_id, "strategy_name": name, "underlying": underlying, "direction": sig.direction, "structure": sig.structure, "entry_time": sig.entry_time.isoformat(), "reason": sig.reason}, default=str))
        return
    if args.mode == "tick":
        rows = run_tick(args.config, dry_run=args.dry_run, refresh=args.refresh)
        if not rows:
            print("NO_CHANGE paper-only strategy pack tick")
        else:
            print(json.dumps(rows, indent=2, default=str))
        return


if __name__ == "__main__":
    main()
