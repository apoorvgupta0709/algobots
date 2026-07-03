#!/usr/bin/env python3
"""Backtest BankNifty Constituent-Led Pullback Continuation v2.

Research-only. No FYERS order APIs are called. This backtest uses stored FYERS
index + constituent candles and simulates the option leg with the same
index-to-option beta risk model used by the paper scanner. Historical expired
BankNifty option-chain candles are not available in the current FYERS master, so
results are labelled PROXY rather than option-candle-grade.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import psycopg
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.banknifty_options_paper import (
    IST,
    CampaignConfig,
    ConfirmationDecision,
    IndexStructureSignal,
    candle_decimal,
    compute_mfe_ratchet_stop,
    confluence_levels_from_candles,
    evaluate_chop_regime,
    evaluate_lunch_chop_guard,
    evaluate_pullback_continuation,
    load_config,
    parse_time,
    size_lots_by_risk,
)

REPORT_DIR = PROJECT_ROOT / "reports"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "banknifty_options_paper.json"
TWO_PLACES = Decimal("0.01")
# Round-trip cost per 1-lot BankNifty option trade (brokerage + slippage + spread crossing).
TRADE_COST_RUPEES = Decimal("100")


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume}


@dataclass
class Trade:
    day: date
    direction: str
    entry_ts: datetime
    exit_ts: datetime
    entry_index: Decimal
    exit_index: Decimal
    reference_level: Decimal
    index_stop: Decimal
    beta: Decimal
    strike_rank: str
    lots: int
    quantity: int
    risk_rupees: Decimal
    pnl_rupees: Decimal
    pnl_r: Decimal
    mfe_rupees: Decimal
    mfe_r: Decimal
    capture_pct: Decimal
    exit_reason: str
    minutes_open: int


def money(v: Decimal) -> str:
    return f"₹{v.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def pct(v: Decimal) -> str:
    return f"{v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def connect_db() -> psycopg.Connection:
    return psycopg.connect(os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker"))


def fetch_candles(conn: psycopg.Connection, symbols: list[str], resolution: str, start: date, end: date) -> dict[str, list[Candle]]:
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
        for symbol, ts, o, h, l, c, v in cur.fetchall():
            out[symbol].append(Candle(ts=ts, open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(l)), close=Decimal(str(c)), volume=int(v or 0)))
        return dict(out)


def by_ist_day(candles: Iterable[Candle]) -> dict[date, list[Candle]]:
    days: dict[date, list[Candle]] = defaultdict(list)
    for c in candles:
        days[c.ts.astimezone(IST).date()].append(c)
    return {d: sorted(rows, key=lambda c: c.ts) for d, rows in days.items()}


def slot_key(ts: datetime) -> tuple[int, int]:
    t = ts.astimezone(IST).time()
    return (t.hour, t.minute)


def index_relvol_for_slot(day_candle: Candle, prior_days: list[list[Candle]]) -> Decimal | None:
    vals = []
    key = slot_key(day_candle.ts)
    for rows in prior_days[-20:]:
        for c in rows:
            if slot_key(c.ts) == key and c.volume > 0:
                vals.append(Decimal(c.volume))
                break
    if not vals:
        return None
    avg = sum(vals, Decimal("0")) / Decimal(len(vals))
    if avg <= 0:
        return None
    return (Decimal(day_candle.volume) / avg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def cumulative_vwap(rows: list[Candle]) -> Decimal | None:
    vol = sum(Decimal(c.volume) for c in rows if c.volume > 0)
    if vol <= 0:
        return None
    pv = sum(((c.high + c.low + c.close) / Decimal("3")) * Decimal(c.volume) for c in rows if c.volume > 0)
    return pv / vol


def relvol_symbol(rows_by_day: dict[date, list[Candle]], day: date, upto_idx: int, prior_dates: list[date]) -> Decimal | None:
    rows = rows_by_day.get(day, [])
    if upto_idx >= len(rows):
        return None
    cur_vol = Decimal(rows[upto_idx].volume)
    vals = []
    key = slot_key(rows[upto_idx].ts)
    for pd in prior_dates[-20:]:
        prow = rows_by_day.get(pd, [])
        for c in prow:
            if slot_key(c.ts) == key and c.volume > 0:
                vals.append(Decimal(c.volume))
                break
    if not vals:
        return None
    avg = sum(vals, Decimal("0")) / Decimal(len(vals))
    if avg <= 0:
        return None
    return (cur_vol / avg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def direction_layer(
    *,
    config: CampaignConfig,
    day: date,
    idx: int,
    index_rows: list[Candle],
    constituent_days: dict[str, dict[date, list[Candle]]],
    weights: dict[str, Decimal],
    prior_dates: list[date],
) -> tuple[str | None, list[str]]:
    if idx >= len(index_rows):
        return None, ["missing index candle"]
    index_open = index_rows[0].open
    index_close = index_rows[idx].close
    index_pct = (index_close - index_open) / index_open * Decimal("100") if index_open > 0 else Decimal("0")
    bullish_index = index_pct >= config.min_index_confirmation_pct
    bearish_index = index_pct <= -config.min_index_confirmation_pct
    if not (bullish_index or bearish_index):
        return None, [f"index confirmation {index_pct:.4f}% below threshold"]

    covered = Decimal("0")
    pos_w = Decimal("0")
    neg_w = Decimal("0")
    vwap_above_w = Decimal("0")
    vwap_below_w = Decimal("0")
    top_confirm = 0
    moves: list[tuple[Decimal, str]] = []

    for symbol, rows_by_day in constituent_days.items():
        rows = rows_by_day.get(day, [])
        if idx >= len(rows) or not rows:
            continue
        w = weights.get(symbol, Decimal("0"))
        if w <= 0:
            continue
        covered += w
        open_ = rows[0].open
        close = rows[idx].close
        move = (close - open_) / open_ * Decimal("100") if open_ > 0 else Decimal("0")
        if move > 0:
            pos_w += w
        elif move < 0:
            neg_w += w
        vw = cumulative_vwap(rows[: idx + 1])
        if vw is not None:
            if close >= vw:
                vwap_above_w += w
            if close <= vw:
                vwap_below_w += w
        rv = relvol_symbol(rows_by_day, day, idx, prior_dates)
        if vw is not None and rv is not None and rv >= config.relative_volume_threshold:
            if (move > 0 and close >= vw) or (move < 0 and close <= vw):
                moves.append((abs(move) * w, symbol))
                top_confirm += 1

    if covered <= 0:
        return None, ["no constituent coverage"]
    coverage_pct = covered
    if coverage_pct < config.min_constituent_coverage_pct:
        return None, [f"coverage {coverage_pct}% < {config.min_constituent_coverage_pct}%"]

    bullish_breadth = pos_w >= config.min_directional_weight_pct
    bearish_breadth = neg_w >= config.min_directional_weight_pct
    direction: str | None = None
    if bullish_index and bullish_breadth:
        direction = "CE"
    elif bearish_index and bearish_breadth:
        direction = "PE"
    else:
        return None, [f"breadth/index disagreement: index {index_pct:.2f}%, pos_w {pos_w:.2f}, neg_w {neg_w:.2f}"]

    if top_confirm < config.min_vwap_volume_confirming_top_movers:
        return None, ["top mover VWAP/rel-vol confirmation missing"]
    side_w = vwap_above_w if direction == "CE" else vwap_below_w
    if side_w < config.weighted_vwap_side_pct:
        return None, [f"weighted VWAP side {side_w:.2f}% < {config.weighted_vwap_side_pct}%"]
    return direction, [f"direction {direction}: index {index_pct:.2f}%, pos_w {pos_w:.2f}, neg_w {neg_w:.2f}, vwap_side {side_w:.2f}%"]


def day_range_so_far(rows: list[Candle], idx: int) -> tuple[Decimal, Decimal]:
    upto = rows[: idx + 1]
    return max(c.high for c in upto), min(c.low for c in upto)


def adr10(prior_rows: list[list[Candle]]) -> Decimal | None:
    vals = []
    for rows in prior_rows[-10:]:
        if rows:
            vals.append(max(c.high for c in rows) - min(c.low for c in rows))
    if not vals:
        return None
    return sum(vals, Decimal("0")) / Decimal(len(vals))


def next_minute_open(minute_rows: list[Candle], at_or_after: datetime) -> Candle | None:
    """First 1-minute candle starting at or after the given timestamp.

    Callers must pass the signal candle's *close* time (start + resolution):
    the signal uses the completed 5m candle, so filling any earlier than its
    close is look-ahead.
    """
    for c in minute_rows:
        if c.ts >= at_or_after:
            return c
    return None


def simulate_trade(
    *,
    config: CampaignConfig,
    day: date,
    direction: str,
    signal_ts: datetime,
    entry_candle: Candle,
    minute_rows: list[Candle],
    reference_level: Decimal,
    index_stop: Decimal,
    rank: str,
    beta: Decimal,
    daily_realized: Decimal,
    round_trip_cost: Decimal = TRADE_COST_RUPEES,
    cost_aware_breakeven: bool = True,
) -> Trade | None:
    entry = entry_candle.open
    index_distance = abs(entry - index_stop)
    # Proxy premium is only used for sizing/exposure mechanics; P&L is beta * index move * quantity.
    proxy_premium = Decimal("1000") if rank == "ATM" else Decimal("700")
    est_stop_premium = proxy_premium - (index_distance * beta)
    lots, qty, _exposure, risk_rupees = size_lots_by_risk(
        entry_premium=proxy_premium,
        estimated_stop_premium=est_stop_premium,
        lot_size=30,
        max_trade_loss=config.max_trade_loss,
        max_premium_exposure=config.max_premium_exposure,
    )
    if lots < 1:
        return None
    # Don't open if one full loss would breach daily cap.
    if daily_realized - risk_rupees < -config.max_daily_loss:
        return None

    signed = Decimal("1") if direction == "CE" else Decimal("-1")
    lock: Decimal | None = None
    best_pnl = Decimal("0")
    exit_ts = minute_rows[-1].ts
    exit_index = minute_rows[-1].close
    exit_reason = "force_intraday_exit"
    pnl = Decimal("0")
    force_time = time(15, 20)

    tracking = [c for c in minute_rows if c.ts >= entry_candle.ts]
    if not tracking:
        return None
    for i, c in enumerate(tracking):
        local_time = c.ts.astimezone(IST).time()
        move_close = signed * (c.close - entry)
        pnl = (move_close * beta * Decimal(qty)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        # Conservative intraminute stop using low/high in the adverse direction.
        if direction == "CE" and c.low <= index_stop:
            pnl = -risk_rupees
            exit_ts, exit_index, exit_reason = c.ts, index_stop, "index_structure_stop"
            break
        if direction == "PE" and c.high >= index_stop:
            pnl = -risk_rupees
            exit_ts, exit_index, exit_reason = c.ts, index_stop, "index_structure_stop"
            break
        # Test against the lock built from PRIOR candles only — raising the lock
        # from this candle's high and then filling at this candle's close assumes
        # an intrabar high-before-close ordering that favors the strategy.
        if lock is not None and pnl <= lock:
            pnl = lock
            exit_ts, exit_index, exit_reason = c.ts, c.close, "mfe_ratchet_stop"
            break
        favorable = signed * ((c.high if direction == "CE" else c.low) - entry)
        best_pnl = max(best_pnl, (favorable * beta * Decimal(qty)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))
        # Reuse configured ratchet math by mapping rupee pnl to a fake premium path.
        fake_entry = Decimal("100")
        fake_high = fake_entry + (best_pnl / Decimal(qty))
        fake_stop = compute_mfe_ratchet_stop(
            fake_entry,
            fake_high,
            qty,
            risk_rupees=risk_rupees,
            breakeven_at_r=config.breakeven_at_r,
            ratchet_start_r=config.ratchet_start_r,
            ratchet_giveback_pct=config.ratchet_giveback_pct,
            ratchet_giveback_min_inr=config.ratchet_giveback_min_inr,
            tick_size=config.option_tick_size,
            round_trip_cost_inr=round_trip_cost if cost_aware_breakeven else Decimal("0"),
        )
        if fake_stop is not None:
            candidate_lock = ((fake_stop - fake_entry) * Decimal(qty)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            lock = max(lock or candidate_lock, candidate_lock)
        if c.ts - entry_candle.ts >= timedelta(minutes=config.stagnation_minutes):
            momentum_gone = c.close < reference_level if direction == "CE" else c.close > reference_level
            if pnl < risk_rupees * config.stagnation_min_r and momentum_gone:
                exit_ts, exit_index, exit_reason = c.ts, c.close, "stagnation_exit"
                break
        if local_time >= force_time:
            exit_ts, exit_index, exit_reason = c.ts, c.close, "force_intraday_exit"
            break

    pnl = (pnl - round_trip_cost).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    pnl_r = (pnl / risk_rupees).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if risk_rupees else Decimal("0")
    mfe_r = (best_pnl / risk_rupees).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if risk_rupees else Decimal("0")
    capture = (pnl / best_pnl * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if best_pnl > 0 and pnl > 0 else Decimal("0")
    return Trade(
        day=day,
        direction=direction,
        entry_ts=entry_candle.ts,
        exit_ts=exit_ts,
        entry_index=entry,
        exit_index=exit_index,
        reference_level=reference_level,
        index_stop=index_stop,
        beta=beta,
        strike_rank=rank,
        lots=lots,
        quantity=qty,
        risk_rupees=risk_rupees,
        pnl_rupees=pnl,
        pnl_r=pnl_r,
        mfe_rupees=best_pnl,
        mfe_r=mfe_r,
        capture_pct=capture,
        exit_reason=exit_reason,
        minutes_open=int((exit_ts - entry_candle.ts).total_seconds() // 60),
    )


def run_backtest(
    config: CampaignConfig,
    start: date,
    end: date,
    *,
    round_trip_cost: Decimal = TRADE_COST_RUPEES,
    cost_aware_breakeven: bool = True,
) -> tuple[list[Trade], dict[str, Any]]:
    symbols = [config.underlying_symbol] + [c.fyers_symbol for c in config.constituents]
    weights = {c.fyers_symbol: (c.weight or Decimal("0")) for c in config.constituents}
    with connect_db() as conn:
        five = fetch_candles(conn, symbols, "5", start, end)
        one = fetch_candles(conn, [config.underlying_symbol], "1", start, end)
    index_days = by_ist_day(five.get(config.underlying_symbol, []))
    index_min_days = by_ist_day(one.get(config.underlying_symbol, []))
    constituent_days = {sym: by_ist_day(five.get(sym, [])) for sym in weights}
    dates = sorted(index_days)
    trades: list[Trade] = []
    no_trade_days = 0
    rejection_counts: dict[str, int] = defaultdict(int)

    for di, d in enumerate(dates):
        rows = index_days[d]
        minute_rows = index_min_days.get(d, [])
        if not rows or not minute_rows:
            no_trade_days += 1
            continue
        prior_dates = dates[:di]
        prior_index_rows = [index_days[pd] for pd in prior_dates if pd in index_days]
        day_trades = 0
        daily_realized = Decimal("0")
        open_until: datetime | None = None
        burned: set[tuple[str, int]] = set()
        trade_today = False
        for idx, c in enumerate(rows):
            local_t = c.ts.astimezone(IST).time()
            if local_t < config.no_new_trades_before or local_t > config.no_new_trades_after:
                continue
            if open_until is not None and c.ts <= open_until:
                continue
            if day_trades >= config.max_trades_per_day or daily_realized <= -config.max_daily_loss:
                continue
            direction, reasons = direction_layer(
                config=config,
                day=d,
                idx=idx,
                index_rows=rows,
                constituent_days=constituent_days,
                weights=weights,
                prior_dates=prior_dates,
            )
            if direction is None:
                rejection_counts[reasons[0].split(":", 1)[0]] += 1
                continue
            rv = index_relvol_for_slot(c, prior_index_rows)
            hi, lo = day_range_so_far(rows, idx)
            lunch = evaluate_lunch_chop_guard(
                c.ts,
                day_high=hi,
                day_low=lo,
                adr10=adr10(prior_index_rows),
                index_rel_volume=rv,
                window_start=config.lunch_window_start,
                window_end=config.lunch_window_end,
                min_range_vs_adr=config.lunch_min_day_range_vs_adr10,
                min_relvol=config.lunch_min_relvol,
            )
            if not lunch.allowed:
                rejection_counts["lunch_chop"] += 1
                continue
            chop = evaluate_chop_regime([x.as_dict() for x in rows[: idx + 1]], lookback_candles=config.chop_lookback_candles, max_net_move_pct=config.chop_max_net_move_pct, max_vwap_crosses=config.chop_max_vwap_crosses)
            if not chop.allowed:
                rejection_counts["chop_regime"] += 1
                continue
            candles_so_far = [x.as_dict() for x in rows[: idx + 1]]
            levels = confluence_levels_from_candles(candles_so_far, direction=direction, structure_lookback=config.index_structure_lookback_candles)
            pb = evaluate_pullback_continuation(
                direction=direction,
                candles=candles_so_far,
                confluence_levels=levels,
                breakout_buffer_pct=config.index_structure_breakout_buffer_pct,
                level_hold_buffer_pct=config.pullback_level_hold_buffer_pct,
                structure_stop_buffer_pct=config.index_structure_stop_buffer_pct,
                leg_lookback_candles=config.leg_lookback_candles,
                max_pullback_candles=config.pullback_max_candles,
            )
            if not pb.confirmed or pb.stop_level is None or pb.reference_level is None:
                rejection_counts["pullback_not_confirmed"] += 1
                continue
            level_key = (direction, int((pb.reference_level / Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP)))
            if level_key in burned:
                rejection_counts["burned_level"] += 1
                continue
            entry = next_minute_open(minute_rows, c.ts + timedelta(minutes=5))
            if entry is None:
                continue
            trade = None
            for rank, beta in (("ATM", config.beta_fallback_atm), ("OTM1", config.beta_fallback_otm1)):
                trade = simulate_trade(
                    config=config,
                    day=d,
                    direction=direction,
                    signal_ts=c.ts,
                    entry_candle=entry,
                    minute_rows=minute_rows,
                    reference_level=pb.reference_level,
                    index_stop=pb.stop_level,
                    rank=rank,
                    beta=beta,
                    daily_realized=daily_realized,
                    round_trip_cost=round_trip_cost,
                    cost_aware_breakeven=cost_aware_breakeven,
                )
                if trade is not None:
                    break
            if trade is None:
                rejection_counts["risk_over_cap"] += 1
                continue
            trades.append(trade)
            trade_today = True
            day_trades += 1
            daily_realized += trade.pnl_rupees
            open_until = trade.exit_ts
            if trade.exit_reason == "index_structure_stop" and trade.pnl_r <= Decimal("-0.95"):
                burned.add(level_key)
        if not trade_today:
            no_trade_days += 1

    meta = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "trading_days": len(dates),
        "no_trade_days": no_trade_days,
        "rejection_counts": dict(sorted(rejection_counts.items(), key=lambda kv: kv[1], reverse=True)[:12]),
        "exit_params": {
            "breakeven_at_r": str(config.breakeven_at_r),
            "ratchet_start_r": str(config.ratchet_start_r),
            "ratchet_giveback_pct": str(config.ratchet_giveback_pct),
            "ratchet_giveback_min_inr": str(config.ratchet_giveback_min_inr),
            "round_trip_cost_inr": str(round_trip_cost),
            "cost_aware_breakeven": cost_aware_breakeven,
        },
    }
    return trades, meta


def summarize(trades: list[Trade], meta: dict[str, Any], *, max_daily_loss: Decimal = Decimal("5000")) -> dict[str, Any]:
    n = len(trades)
    wins = [t for t in trades if t.pnl_rupees > 0]
    losses = [t for t in trades if t.pnl_rupees < 0]
    pnl = sum((t.pnl_rupees for t in trades), Decimal("0"))
    expectancy = (sum((t.pnl_r for t in trades), Decimal("0")) / Decimal(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if n else Decimal("0")
    avg_win_r = (sum((t.pnl_r for t in wins), Decimal("0")) / Decimal(len(wins))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if wins else Decimal("0")
    avg_loss_r = (sum((t.pnl_r for t in losses), Decimal("0")) / Decimal(len(losses))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if losses else Decimal("0")
    captures = [t.capture_pct for t in trades if t.mfe_rupees > 0]
    avg_capture = (sum(captures, Decimal("0")) / Decimal(len(captures))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if captures else Decimal("0")
    by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for t in trades:
        by_day[t.day] += t.pnl_rupees
    equity = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for d in sorted(by_day):
        equity += by_day[d]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    days_hit_cap = sum(1 for v in by_day.values() if v <= -max_daily_loss)
    return {
        **meta,
        "max_daily_loss": max_daily_loss,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (Decimal(len(wins)) / Decimal(n) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if n else Decimal("0"),
        "total_pnl": pnl.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        "expectancy_r": expectancy,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "avg_mfe_capture_pct": avg_capture,
        "max_drawdown": max_dd.quantize(TWO_PLACES, rounding=ROUND_HALF_UP),
        "days_hit_5k_cap": days_hit_cap,
        "stagnation_exits": sum(1 for t in trades if t.exit_reason == "stagnation_exit"),
        "exit_counts": dict(sorted({r: sum(1 for t in trades if t.exit_reason == r) for r in set(t.exit_reason for t in trades)}.items())),
    }


def write_outputs(trades: list[Trade], summary: dict[str, Any], *, experimental: bool = False) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    tag = "experimental_" if experimental else ""
    csv_path = REPORT_DIR / f"banknifty_pullback_v2_proxy_{tag}trades_{stamp}.csv"
    md_path = REPORT_DIR / f"banknifty_pullback_v2_proxy_{tag}backtest_{stamp}.md"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "day", "direction", "entry_ts", "exit_ts", "entry_index", "exit_index", "reference_level", "index_stop", "beta", "strike_rank", "lots", "quantity", "risk_rupees", "pnl_rupees", "pnl_r", "mfe_rupees", "mfe_r", "capture_pct", "exit_reason", "minutes_open",
        ])
        writer.writeheader()
        for t in trades:
            writer.writerow({k: getattr(t, k) for k in writer.fieldnames})
    ep = summary.get("exit_params", {})
    lines = [
        "# BankNifty Pullback Continuation v2 — Proxy Backtest"
        + (" (EXPERIMENTAL exit-sweep — NOT promoted to active config)" if experimental else ""),
        "",
        "Research-only; no orders placed.",
        "",
    ]
    if experimental:
        lines += [
            "> **EXPERIMENTAL**: this run overrides exit parameters and/or cost-aware "
            "breakeven for research. Parameters here are NOT promoted to the active "
            "config and must clear the acceptance gates below before any promotion.",
            "",
        ]
    if ep:
        lines += [
            "## Exit parameters used",
            "",
            f"- breakeven_at_r: {ep.get('breakeven_at_r')}",
            f"- ratchet_start_r: {ep.get('ratchet_start_r')}",
            f"- ratchet_giveback_pct: {ep.get('ratchet_giveback_pct')}",
            f"- ratchet_giveback_min_inr: {ep.get('ratchet_giveback_min_inr')}",
            f"- round_trip_cost_inr: {ep.get('round_trip_cost_inr')}",
            f"- cost_aware_breakeven: {ep.get('cost_aware_breakeven')}",
            "",
        ]
    cost_used = ep.get("round_trip_cost_inr", str(TRADE_COST_RUPEES)) if ep else str(TRADE_COST_RUPEES)
    lines += [
        "## Data caveat",
        "",
        "Stored 1-min/5-min BankNifty index and constituent candles were used. Historical expired BankNifty option-chain candles are not available in the current FYERS master, so the option leg is simulated with the configured index-to-option beta risk model. Treat this as signal/risk validation, not final option-candle acceptance.",
        "",
        f"Cost model: \u20b9{cost_used} round-trip per trade (brokerage + slippage), subtracted from every trade; breakeven/ratchet lock is cost-aware unless --legacy-gross-breakeven is set.",
        "",
        "## Summary",
    ]
    labels = [
        ("Window", f"{summary['start']} to {summary['end']}"),
        ("Trading days", str(summary["trading_days"])),
        ("No-trade days", str(summary["no_trade_days"])),
        ("Trades", str(summary["trades"])),
        ("Win rate", f"{summary['win_rate_pct']}%"),
        ("Total P&L", money(summary["total_pnl"])),
        ("Expectancy", f"{summary['expectancy_r']}R"),
        ("Avg win", f"{summary['avg_win_r']}R"),
        ("Avg loss", f"{summary['avg_loss_r']}R"),
        ("Avg MFE capture", f"{summary['avg_mfe_capture_pct']}%"),
        ("Max drawdown", money(summary["max_drawdown"])),
        ("Days hitting ₹5k cap", str(summary["days_hit_5k_cap"])),
        ("Stagnation exits", str(summary["stagnation_exits"])),
    ]
    for k, v in labels:
        lines.append(f"- **{k}:** {v}")
    lines.extend(["", "## Acceptance gates", ""])
    gates = [
        ("≥ 40 trades", summary["trades"] >= 40),
        ("Expectancy ≥ +0.15R", summary["expectancy_r"] >= Decimal("0.15")),
        ("MFE capture ≥ 55%", summary["avg_mfe_capture_pct"] >= Decimal("55")),
        ("Max DD < 3 daily caps", summary["max_drawdown"] > Decimal("-3") * Decimal(str(summary.get("max_daily_loss", "5000")))),
    ]
    for name, ok in gates:
        lines.append(f"- {'PASS' if ok else 'FAIL'} — {name}")
    lines.extend(["", "## Exit counts", ""])
    for reason, count in summary["exit_counts"].items():
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Top rejection counts", ""])
    for reason, count in summary["rejection_counts"].items():
        lines.append(f"- {reason}: {count}")
    lines.extend(["", f"Trade CSV: `{csv_path}`", ""])
    md_path.write_text("\n".join(lines))
    return md_path, csv_path


def validate_experimental_inputs(
    *,
    breakeven_at_r: Decimal | None,
    ratchet_start_r: Decimal | None,
    ratchet_giveback_pct: Decimal | None,
    ratchet_giveback_min_inr: Decimal | None,
    round_trip_cost: Decimal,
) -> None:
    """Reject non-sensible experimental exit-sweep inputs before backtests run."""
    if round_trip_cost < 0:
        raise SystemExit(
            "Refusing: --round-trip-cost must be >= 0 "
            "(a negative cost would add fake profit to every trade)"
        )
    if breakeven_at_r is not None and breakeven_at_r <= 0:
        raise SystemExit("Refusing: --breakeven-at-r must be > 0 when provided")
    if ratchet_start_r is not None and ratchet_start_r <= 0:
        raise SystemExit("Refusing: --ratchet-start-r must be > 0 when provided")
    if ratchet_giveback_pct is not None and ratchet_giveback_pct <= 0:
        raise SystemExit("Refusing: --ratchet-giveback-pct must be > 0 when provided")
    if ratchet_giveback_min_inr is not None and ratchet_giveback_min_inr < 0:
        raise SystemExit("Refusing: --ratchet-giveback-min-inr must be >= 0 when provided")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest BankNifty pullback_continuation_v2 using stored candles; research-only.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    # Experimental exit-sweep overrides. These NEVER touch the active config file:
    # they replace exit fields on an in-memory config copy and tag the report
    # "experimental". Promote to config only after acceptance gates pass.
    parser.add_argument("--breakeven-at-r", type=Decimal, default=None, help="Experimental: override exits.breakeven_at_r")
    parser.add_argument("--ratchet-start-r", type=Decimal, default=None, help="Experimental: arm the MFE ratchet earlier/later than active config")
    parser.add_argument("--ratchet-giveback-pct", type=Decimal, default=None, help="Experimental: override exits.ratchet_giveback_pct")
    parser.add_argument("--ratchet-giveback-min-inr", type=Decimal, default=None, help="Experimental: override exits.ratchet_giveback_min_inr")
    parser.add_argument("--round-trip-cost", type=Decimal, default=TRADE_COST_RUPEES, help=f"Round-trip cost per trade in INR (default {TRADE_COST_RUPEES})")
    parser.add_argument("--legacy-gross-breakeven", action="store_true", help="Disable cost-aware breakeven (lock gross-flat, the pre-fix behavior)")
    args = parser.parse_args()
    config = load_config(args.config)
    if not config.paper_only or config.live_orders_enabled:
        raise SystemExit("Refusing: config must be paper_only true and live_orders_enabled false")

    validate_experimental_inputs(
        breakeven_at_r=args.breakeven_at_r,
        ratchet_start_r=args.ratchet_start_r,
        ratchet_giveback_pct=args.ratchet_giveback_pct,
        ratchet_giveback_min_inr=args.ratchet_giveback_min_inr,
        round_trip_cost=args.round_trip_cost,
    )

    overrides: dict[str, Decimal] = {}
    if args.breakeven_at_r is not None:
        overrides["breakeven_at_r"] = args.breakeven_at_r
    if args.ratchet_start_r is not None:
        overrides["ratchet_start_r"] = args.ratchet_start_r
    if args.ratchet_giveback_pct is not None:
        overrides["ratchet_giveback_pct"] = args.ratchet_giveback_pct
    if args.ratchet_giveback_min_inr is not None:
        overrides["ratchet_giveback_min_inr"] = args.ratchet_giveback_min_inr
    if overrides:
        config = replace(config, **overrides)
    cost_aware = not args.legacy_gross_breakeven
    experimental = bool(overrides) or args.round_trip_cost != TRADE_COST_RUPEES or args.legacy_gross_breakeven

    trades, meta = run_backtest(
        config,
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        round_trip_cost=args.round_trip_cost,
        cost_aware_breakeven=cost_aware,
    )
    summary = summarize(trades, meta, max_daily_loss=config.max_daily_loss)
    md_path, csv_path = write_outputs(trades, summary, experimental=experimental)
    print(json.dumps({k: str(v) if isinstance(v, Decimal) else v for k, v in summary.items()}, indent=2, default=str))
    print(f"Report: {md_path}")
    print(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
