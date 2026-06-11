#!/usr/bin/env python3
"""Generate a read-only daily market report from quotes and factor snapshots.

The report is decision support only. It does not place, modify, or cancel orders.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Sequence

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = "postgresql://" + "hermes" + "@" + "127.0.0.1" + ":55432" + "/finance_tracker"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
TWO_PLACES = Decimal("0.01")
FOUR_PLACES = Decimal("0.0001")
STALE_FACTOR_AFTER = timedelta(days=5)
STALE_QUOTE_AFTER = timedelta(hours=36)


@dataclass(frozen=True)
class ReportRow:
    symbol: str
    resolution: str
    factor_ts: datetime
    quote_updated_at: datetime | None
    quote_time: datetime | None
    ltp: Decimal | None
    previous_close: Decimal | None
    candle_close: Decimal | None
    trend: str | None
    sma_20: Decimal | None
    sma_50: Decimal | None
    sma_200: Decimal | None
    ema_20: Decimal | None
    rsi_14: Decimal | None
    atr_pct_14: Decimal | None
    relative_volume_20: Decimal | None
    volatility_regime: str | None
    macd_12_26: Decimal | None
    macd_signal_9: Decimal | None
    macd_histogram: Decimal | None
    roc_20: Decimal | None
    roc_60: Decimal | None
    donchian_20_high: Decimal | None
    donchian_20_low: Decimal | None
    donchian_55_high: Decimal | None
    donchian_55_low: Decimal | None
    previous_day_high: Decimal | None
    previous_day_low: Decimal | None
    previous_day_close: Decimal | None
    gap_pct: Decimal | None
    breakout_20: str | None
    breakout_55: str | None


def as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def format_inr(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"₹{value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}"


def format_pct(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{(value * Decimal('100')).quantize(TWO_PLACES, rounding=ROUND_HALF_UP):,.2f}%"


def format_decimal(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    rounded = value.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral():
        return str(rounded.to_integral())
    return format(rounded.normalize(), "f")


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "n/a"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def quote_change(row: ReportRow) -> tuple[Decimal | None, Decimal | None]:
    if row.ltp is None or row.previous_close is None or row.previous_close == 0:
        return None, None
    change = row.ltp - row.previous_close
    pct = change / row.previous_close
    return change, pct


def quote_freshness_time(row: ReportRow) -> datetime | None:
    """Prefer market quote time for freshness; fall back to DB write time."""
    return row.quote_time or row.quote_updated_at


def age_text(value: datetime | None, generated_at: datetime) -> str:
    if value is None:
        return "missing"
    age = generated_at - value.astimezone(timezone.utc)
    if age < timedelta(0):
        return "future timestamp"
    if age >= timedelta(days=2):
        return f"{age.days}d old"
    hours = int(age.total_seconds() // 3600)
    return f"{hours}h old"


def freshness_flags(row: ReportRow, generated_at: datetime | None = None) -> list[str]:
    generated_at = generated_at or datetime.now(timezone.utc)
    flags: list[str] = []
    factor_age = generated_at - row.factor_ts.astimezone(timezone.utc)
    if factor_age > STALE_FACTOR_AFTER:
        flags.append(f"Data freshness: factor snapshot is {age_text(row.factor_ts, generated_at)}")
    quote_time = quote_freshness_time(row)
    if quote_time is None:
        flags.append("Data freshness: latest quote is missing")
    else:
        quote_age = generated_at - quote_time.astimezone(timezone.utc)
        if quote_age > STALE_QUOTE_AFTER:
            flags.append(f"Data freshness: quote snapshot is {age_text(quote_time, generated_at)}")
    return flags


def freshness_summary(row: ReportRow, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    return f"factor {age_text(row.factor_ts, generated_at)}; quote {age_text(quote_freshness_time(row), generated_at)}"


def resolution_candle_label(resolution: str, *, capitalize: bool = False) -> str:
    if resolution == "D":
        label = "daily candle"
    elif resolution == "W":
        label = "weekly candle"
    elif resolution == "M":
        label = "monthly candle"
    else:
        label = f"{resolution}-minute candle"
    return label.capitalize() if capitalize else label


def classify_flags(row: ReportRow, generated_at: datetime | None = None) -> list[str]:
    flags: list[str] = freshness_flags(row, generated_at)
    candle_label = resolution_candle_label(row.resolution)
    if row.trend == "bullish":
        flags.append(f"Bullish {candle_label} trend: close above SMA20/SMA50")
    elif row.trend == "bearish":
        flags.append(f"Bearish {candle_label} trend: close below SMA20/SMA50")
    elif row.trend == "neutral":
        flags.append(f"Neutral {candle_label} trend: mixed SMA structure")

    if row.rsi_14 is not None:
        if row.rsi_14 >= Decimal("70"):
            flags.append(f"Momentum extended: RSI {format_decimal(row.rsi_14)}")
        elif row.rsi_14 <= Decimal("35"):
            flags.append(f"Weak momentum / possible oversold watch: RSI {format_decimal(row.rsi_14)}")

    breakout_labels = [
        ("20-day", row.breakout_20),
        ("55-day", row.breakout_55),
    ]
    fresh_breakouts = [label for label, value in breakout_labels if value == "yes"]
    if fresh_breakouts:
        flags.append(f"Breakout watch: close above prior {' and '.join(fresh_breakouts)} range")

    if row.macd_histogram is not None:
        if row.macd_histogram > Decimal("0"):
            flags.append(f"MACD positive momentum: histogram {format_decimal(row.macd_histogram)}")
        elif row.macd_histogram < Decimal("0"):
            flags.append(f"MACD negative momentum: histogram {format_decimal(row.macd_histogram)}")

    if row.gap_pct is not None and abs(row.gap_pct) >= Decimal("0.02"):
        flags.append(f"Gap risk: opening gap {format_pct(row.gap_pct)}")

    if row.atr_pct_14 is not None:
        if row.atr_pct_14 >= Decimal("0.03"):
            flags.append(f"High volatility: ATR% {format_pct(row.atr_pct_14)}")
        elif row.atr_pct_14 <= Decimal("0.01"):
            flags.append(f"Low volatility: ATR% {format_pct(row.atr_pct_14)}")

    if row.relative_volume_20 is not None:
        if row.relative_volume_20 >= Decimal("1.5"):
            flags.append(f"Volume expansion: relative volume {format_decimal(row.relative_volume_20)}")
        elif row.relative_volume_20 <= Decimal("0.5"):
            flags.append(f"Low participation: relative volume {format_decimal(row.relative_volume_20)}")

    return flags or ["No major technical flags from current factor set"]


def fetch_report_rows(
    conn: psycopg.Connection,
    symbols: Sequence[str] | None,
    limit: int,
    resolution: str | None = None,
) -> list[ReportRow]:
    filters = ["source = 'technical_factor_engine'"]
    params: list[object] = []
    if symbols:
        filters.append("symbol = any(%s)")
        params.append(list(symbols))
    if resolution:
        filters.append("resolution = %s")
        params.append(resolution)
    params.append(limit)
    where_clause = " and ".join(filters)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            with latest_factors as (
                select distinct on (symbol, resolution)
                    symbol, resolution, ts, factors, created_at
                from research.factor_snapshots
                where {where_clause}
                order by symbol, resolution, ts desc, created_at desc
            )
            select
                fs.symbol,
                fs.resolution,
                fs.ts,
                q.updated_at,
                q.quote_time,
                q.ltp,
                q.close as previous_close,
                fs.factors->>'close' as candle_close,
                fs.factors->>'trend' as trend,
                fs.factors->>'sma_20' as sma_20,
                fs.factors->>'sma_50' as sma_50,
                fs.factors->>'sma_200' as sma_200,
                fs.factors->>'ema_20' as ema_20,
                fs.factors->>'rsi_14' as rsi_14,
                fs.factors->>'atr_pct_14' as atr_pct_14,
                fs.factors->>'relative_volume_20' as relative_volume_20,
                fs.factors->>'volatility_regime' as volatility_regime,
                fs.factors->>'macd_12_26' as macd_12_26,
                fs.factors->>'macd_signal_9' as macd_signal_9,
                fs.factors->>'macd_histogram' as macd_histogram,
                fs.factors->>'roc_20' as roc_20,
                fs.factors->>'roc_60' as roc_60,
                fs.factors->>'donchian_20_high' as donchian_20_high,
                fs.factors->>'donchian_20_low' as donchian_20_low,
                fs.factors->>'donchian_55_high' as donchian_55_high,
                fs.factors->>'donchian_55_low' as donchian_55_low,
                fs.factors->>'previous_day_high' as previous_day_high,
                fs.factors->>'previous_day_low' as previous_day_low,
                fs.factors->>'previous_day_close' as previous_day_close,
                fs.factors->>'gap_pct' as gap_pct,
                fs.factors->>'breakout_20' as breakout_20,
                fs.factors->>'breakout_55' as breakout_55
            from latest_factors fs
            left join market.quotes q on q.symbol = fs.symbol
            order by fs.ts desc, fs.symbol
            limit %s
            """,
            params,
        )
        rows = cur.fetchall()

    return [
        ReportRow(
            symbol=row[0],
            resolution=row[1],
            factor_ts=row[2],
            quote_updated_at=row[3],
            quote_time=row[4],
            ltp=as_decimal(row[5]),
            previous_close=as_decimal(row[6]),
            candle_close=as_decimal(row[7]),
            trend=row[8],
            sma_20=as_decimal(row[9]),
            sma_50=as_decimal(row[10]),
            sma_200=as_decimal(row[11]),
            ema_20=as_decimal(row[12]),
            rsi_14=as_decimal(row[13]),
            atr_pct_14=as_decimal(row[14]),
            relative_volume_20=as_decimal(row[15]),
            volatility_regime=row[16],
            macd_12_26=as_decimal(row[17]),
            macd_signal_9=as_decimal(row[18]),
            macd_histogram=as_decimal(row[19]),
            roc_20=as_decimal(row[20]),
            roc_60=as_decimal(row[21]),
            donchian_20_high=as_decimal(row[22]),
            donchian_20_low=as_decimal(row[23]),
            donchian_55_high=as_decimal(row[24]),
            donchian_55_low=as_decimal(row[25]),
            previous_day_high=as_decimal(row[26]),
            previous_day_low=as_decimal(row[27]),
            previous_day_close=as_decimal(row[28]),
            gap_pct=as_decimal(row[29]),
            breakout_20=row[30],
            breakout_55=row[31],
        )
        for row in rows
    ]


def render_report(rows: Sequence[ReportRow], generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    lines: list[str] = [
        "## Daily Market Report",
        f"Generated: {format_dt(generated_at)}",
        "Scope: read-only research report; Not trade advice; no orders placed.",
        "",
    ]
    if not rows:
        lines.extend([
            "## Facts",
            "No factor snapshots found. Run candle ingestion and `compute_technical_factors.py` first.",
            "",
            "## Suggested next actions",
            "- Ingest FYERS candles for watchlist symbols.",
            "- Compute technical factors before generating the report again.",
        ])
        return "\n".join(lines)

    lines.append("## Facts")
    for row in rows:
        change, pct = quote_change(row)
        lines.extend(
            [
                f"- {row.symbol} ({row.resolution})",
                f"  - LTP: {format_inr(row.ltp)}",
                f"  - Change: {format_inr(change)} ({format_pct(pct)})",
                f"  - {resolution_candle_label(row.resolution, capitalize=True)} close: {format_inr(row.candle_close)}",
                f"  - {resolution_candle_label(row.resolution, capitalize=True)} trend: {row.trend or 'n/a'}",
                f"  - SMA20 / SMA50 / SMA200: {format_inr(row.sma_20)} / {format_inr(row.sma_50)} / {format_inr(row.sma_200)}",
                f"  - EMA20: {format_inr(row.ema_20)}",
                f"  - RSI14: {format_decimal(row.rsi_14)}",
                f"  - MACD(12,26): {format_decimal(row.macd_12_26)}; signal {format_decimal(row.macd_signal_9)}; histogram {format_decimal(row.macd_histogram)}",
                f"  - ROC20 / ROC60: {format_pct(row.roc_20)} / {format_pct(row.roc_60)}",
                f"  - Donchian20 range: {format_inr(row.donchian_20_low)} – {format_inr(row.donchian_20_high)}",
                f"  - Gap: {format_pct(row.gap_pct)}",
                f"  - Breakout20 / Breakout55: {row.breakout_20 or 'n/a'} / {row.breakout_55 or 'n/a'}",
                f"  - ATR%14: {format_pct(row.atr_pct_14)}; Volatility: {row.volatility_regime or 'n/a'}",
                f"  - RelVol20: {format_decimal(row.relative_volume_20)}",
                f"  - Factor time: {format_dt(row.factor_ts)}; Quote stored: {format_dt(row.quote_updated_at)}",
                f"  - Data freshness: {freshness_summary(row, generated_at)}",
            ]
        )

    lines.extend(["", "## Risk flags / setups"])
    for row in rows:
        lines.append(f"- {row.symbol}")
        for flag in classify_flags(row, generated_at):
            lines.append(f"  - {flag}")

    lines.extend(
        [
            "",
            "## Suggested next actions",
            "- Refresh FYERS quotes/candles before market decisions.",
            "- Review high-volatility or weak-momentum names manually before creating any trade idea.",
            "- If a setup is worth tracking, record it as `trading.trade_ideas` for review; execution still requires explicit approval.",
        ]
    )
    return "\n".join(lines)


def write_report(text: str, output_path: Path | None) -> Path:
    if output_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        output_path = PROJECT_ROOT / "reports" / f"daily_market_report_{stamp}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate daily market report from factor snapshots and latest quotes")
    parser.add_argument("--symbols", nargs="+", help="Optional symbols to include")
    parser.add_argument("--resolution", help="Optional factor resolution to include, e.g. D")
    parser.add_argument("--limit", type=int, default=25, help="Maximum rows to include")
    parser.add_argument("--output", type=Path, help="Output markdown path; defaults to reports/daily_market_report_YYYY-MM-DD.md")
    parser.add_argument("--print", action="store_true", help="Print report text to stdout as well as writing file")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.resolution is not None and not args.resolution.strip():
        raise SystemExit("--resolution must be non-empty when provided")


def main() -> None:
    args = parse_args()
    validate_args(args)
    with psycopg.connect(DATABASE_URL) as conn:
        rows = fetch_report_rows(conn, args.symbols, args.limit, args.resolution)
    text = render_report(rows)
    path = write_report(text, args.output)
    if args.print:
        print(text)
    print(f"Wrote daily market report: {path}")


if __name__ == "__main__":
    main()
