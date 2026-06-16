#!/usr/bin/env python3
"""Build / refresh the BankNifty daywise trend-pattern library.

Research / paper-only. Reads stored BankNifty index + constituent 5-minute
candles (and option-chain summary context when available) from PostgreSQL,
engineers per-session features, applies the deterministic classifier, attaches
the nearest historical similar days, and upserts everything into
``research.banknifty_day_features`` and ``research.banknifty_day_classifications``.

No FYERS order APIs are imported or called. The dashboard reads these tables via
the read-only role.

Usage:
    uv run python scripts/build_banknifty_trend_pattern_library.py \
        --from 2025-06-01 --to 2026-06-16 --resolution 5 --print
    uv run python scripts/build_banknifty_trend_pattern_library.py --date 2026-06-16 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.banknifty_trend_patterns import (  # noqa: E402
    IST,
    BankNiftyDayFeatures,
    Candle,
    PatternClassification,
    build_day_features,
    classify_day_rules,
    find_nearest_similar_days,
    to_candle,
    validate_pattern_config_safety,
)

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "banknifty_trend_patterns.json"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_pattern_config(path: Path | str = DEFAULT_CONFIG) -> dict[str, Any]:
    """Load the trend-pattern config and merge in constituent symbols+weights
    from the referenced paper-trading config. Refuses unsafe configs."""
    cfg = json.loads(Path(path).read_text())
    validate_pattern_config_safety(cfg)
    constituents_path = PROJECT_ROOT / cfg.get("constituents_config", "config/banknifty_options_paper.json")
    weights: dict[str, Decimal] = {}
    if constituents_path.exists():
        paper = json.loads(constituents_path.read_text())
        for c in paper.get("constituents", []):
            sym = c.get("fyers_symbol")
            if sym:
                weights[sym] = Decimal(str(c.get("weight", "0")))
    cfg["_constituent_weights"] = weights
    return cfg


# --------------------------------------------------------------------------- #
# History window sizing (pure)
# --------------------------------------------------------------------------- #
def history_lookback_calendar_days(config: Mapping[str, Any]) -> int:
    """Calendar days of prior candle history to load before the requested range.

    A single-date (``--date``) daily run still needs prior sessions to compute
    ``prev_close`` (gap), ``range_vs_adr10`` (ADR lookback) and to seed the
    nearest-neighbour similar-day search. Derived from the ADR / realized-vol
    lookbacks (trading days -> calendar days with a weekend/holiday buffer);
    an explicit ``library.history_lookback_calendar_days`` overrides it.
    """
    lib = config.get("library", {})
    explicit = lib.get("history_lookback_calendar_days")
    if explicit is not None:
        return max(0, int(explicit))
    rv = config.get("realized_vol", {})
    trading = max(int(rv.get("adr_lookback_days", 10)), int(rv.get("daily_lookback_days", 10)))
    # ~5 trading days per 7 calendar days, plus a buffer for weekends/holidays.
    return trading * 2 + 7


def effective_fetch_start(start: date, config: Mapping[str, Any]) -> date:
    """Start date for candle/option-chain fetch: the requested ``start`` pushed
    back by the history lookback so prior-day features are available."""
    return start - timedelta(days=history_lookback_calendar_days(config))


# --------------------------------------------------------------------------- #
# Pure grouping / analysis (no DB)
# --------------------------------------------------------------------------- #
def group_by_ist_day(candles: Iterable[Mapping[str, Any] | Candle]) -> dict[date, list[Candle]]:
    days: dict[date, list[Candle]] = defaultdict(list)
    for raw in candles:
        c = to_candle(raw)
        ts = c.ts
        d = ts.astimezone(IST).date() if ts.tzinfo is not None else ts.date()
        days[d].append(c)
    return {d: sorted(rows, key=lambda c: c.ts) for d, rows in days.items()}


def analyze_sessions(
    *,
    config: Mapping[str, Any],
    index_by_day: Mapping[date, Sequence[Candle]],
    constituent_by_day: Mapping[str, Mapping[date, Sequence[Candle]]] | None = None,
    weights: Mapping[str, Decimal] | None = None,
    option_chain_by_day: Mapping[date, Mapping[str, Any]] | None = None,
    prior_library: Sequence[tuple[BankNiftyDayFeatures, PatternClassification]] = (),
) -> list[tuple[BankNiftyDayFeatures, PatternClassification]]:
    """Pure: turn grouped candles into (features, classification) per session and
    attach nearest historical similar days (past sessions only, no look-ahead).

    ``prior_library`` carries already-persisted (features, classification) pairs
    from earlier sessions so that a single-date / short-window run can still find
    similar days from the full library, not only from the in-window history."""
    constituent_by_day = constituent_by_day or {}
    weights = weights or {}
    option_chain_by_day = option_chain_by_day or {}
    dates = sorted(index_by_day)

    built: list[tuple[BankNiftyDayFeatures, PatternClassification]] = []
    for i, d in enumerate(dates):
        rows = index_by_day[d]
        prev_close = None
        if i > 0:
            prior_rows = index_by_day[dates[i - 1]]
            if prior_rows:
                prev_close = prior_rows[-1].close
        prior_days = [index_by_day[pd] for pd in dates[:i]]
        constituents = {
            sym: by_day.get(d, []) for sym, by_day in constituent_by_day.items() if by_day.get(d)
        }
        feats = build_day_features(
            session_date=d.isoformat(),
            candles=rows,
            config=config,
            prev_close=prev_close,
            prior_days_candles=prior_days,
            constituent_candles=constituents or None,
            weights=weights or None,
            option_chain=option_chain_by_day.get(d),
        )
        label = classify_day_rules(feats, config)
        built.append((feats, label))

    # attach similar days using only sessions that precede each target. History =
    # in-window past sessions PLUS persisted library rows from before the target,
    # deduped by date (freshly built in-window rows win over persisted copies).
    lib_by_date = {f.session_date: (f, l) for f, l in prior_library}
    for idx, (feats, label) in enumerate(built):
        in_window = built[:idx]  # strictly past in-window sessions
        seen = {f.session_date for f, _ in in_window}
        history = list(in_window)
        for d, pair in lib_by_date.items():
            if d < feats.session_date and d not in seen:
                history.append(pair)
        similar = find_nearest_similar_days(feats, history, config)
        label.similar_days = [s.to_dict() for s in similar]
    return built


# --------------------------------------------------------------------------- #
# DB record shaping
# --------------------------------------------------------------------------- #
def feature_record(feats: BankNiftyDayFeatures) -> dict[str, Any]:
    """Column->value map for research.banknifty_day_features upsert."""
    return {
        "session_date": feats.session_date,
        "underlying": feats.underlying,
        "underlying_symbol": feats.underlying_symbol,
        "resolution": feats.resolution,
        "open": feats.open,
        "high": feats.high,
        "low": feats.low,
        "close": feats.close,
        "prev_close": feats.prev_close,
        "gap_pct": feats.gap_pct,
        "day_return_pct": feats.day_return_pct,
        "day_range_pct": feats.day_range_pct,
        "orb_high": feats.orb_high,
        "orb_low": feats.orb_low,
        "orb_range_pct": feats.orb_range_pct,
        "orb_break_direction": feats.orb_break_direction,
        "orb_hold": feats.orb_hold,
        "close_location": feats.close_location,
        "vwap_cross_count": feats.vwap_cross_count,
        "vwap_side_pct": feats.vwap_side_pct,
        "realized_vol": feats.realized_vol,
        "range_vs_adr10": feats.range_vs_adr10,
        "mfe_from_open_pct": feats.mfe_from_open_pct,
        "mae_from_open_pct": feats.mae_from_open_pct,
        "day_high_time": feats.day_high_time,
        "day_low_time": feats.day_low_time,
        "weighted_positive_breadth_pct": feats.weighted_positive_breadth_pct,
        "weighted_negative_breadth_pct": feats.weighted_negative_breadth_pct,
        "weighted_vwap_confirm_pct": feats.weighted_vwap_confirm_pct,
        "breadth_divergence": feats.breadth_divergence,
        "top_positive_contributors": json.dumps(feats.top_positive_contributors),
        "top_negative_contributors": json.dumps(feats.top_negative_contributors),
        "atm_iv": feats.atm_iv,
        "iv_regime": feats.iv_regime,
        "pcr": feats.pcr,
        "max_pain_distance_pct": feats.max_pain_distance_pct,
        "option_chain_available": feats.option_chain_available,
        "candle_count": feats.candle_count,
        "segments": json.dumps([s.to_dict() for s in feats.segments]),
        "features": json.dumps(feats.to_feature_dict()),
        "warnings": json.dumps(feats.warnings),
    }


def classification_record(label: PatternClassification) -> dict[str, Any]:
    return {
        "session_date": label.session_date,
        "primary_class": label.primary_class,
        "direction": label.direction,
        "confidence": label.confidence,
        "rule_version": label.rule_version,
        "algorithm": label.algorithm,
        "secondary_tags": label.secondary_tags,
        "explanation": json.dumps(label.explanation),
        "similar_days": json.dumps(label.similar_days),
    }


# --------------------------------------------------------------------------- #
# DB access
# --------------------------------------------------------------------------- #
def connect_db():
    import psycopg

    return psycopg.connect(DATABASE_URL)


def fetch_candles(conn, symbols: Sequence[str], resolution: str, start: date, end: date) -> dict[str, list[Candle]]:
    out: dict[str, list[Candle]] = defaultdict(list)
    with conn.cursor() as cur:
        cur.execute(
            """
            select symbol, ts, open, high, low, close, volume
            from market.candles
            where symbol = any(%s) and resolution = %s and ts::date between %s and %s
            order by symbol, ts
            """,
            (list(symbols), resolution, start, end),
        )
        for sym, ts, o, h, l, c, v in cur.fetchall():
            out[sym].append(Candle(ts=ts, open=Decimal(str(o)), high=Decimal(str(h)),
                                   low=Decimal(str(l)), close=Decimal(str(c)), volume=int(v or 0)))
    return dict(out)


def fetch_option_chain_by_day(conn, underlying: str, start: date, end: date) -> dict[date, dict[str, Any]]:
    """Latest option-chain summary per session date. Missing data is fine."""
    out: dict[date, dict[str, Any]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select distinct on ((snapshot_time at time zone 'Asia/Kolkata')::date)
                       (snapshot_time at time zone 'Asia/Kolkata')::date as d,
                       spot, atm_strike, pcr, max_pain_strike, atm_iv, iv_regime
                from market.option_chain_summary
                where underlying = %s
                  and (snapshot_time at time zone 'Asia/Kolkata')::date between %s and %s
                order by (snapshot_time at time zone 'Asia/Kolkata')::date, snapshot_time desc
                """,
                (underlying, start, end),
            )
            for d, spot, atm_strike, pcr, max_pain, atm_iv, iv_regime in cur.fetchall():
                out[d] = {
                    "spot": spot, "atm_strike": atm_strike, "pcr": pcr,
                    "max_pain_strike": max_pain, "atm_iv": atm_iv, "iv_regime": iv_regime,
                }
    except Exception as exc:  # option-chain table may be empty / absent
        print(f"[warn] option-chain summary unavailable: {exc}", file=sys.stderr)
    return out


def load_persisted_library(
    conn, before: date
) -> list[tuple[BankNiftyDayFeatures, PatternClassification]]:
    """Load already-persisted (features, classification) pairs for sessions strictly
    before ``before`` so a daily run can match similar days against the full library.

    Read-only. Returns ``[]`` (with a warning) if the tables are absent/empty."""
    from psycopg.rows import dict_row

    # imported lazily to avoid a hard import cycle at module load time
    from scripts.generate_banknifty_trend_pattern_report import (
        classification_from_row,
        features_from_row,
    )

    out: list[tuple[BankNiftyDayFeatures, PatternClassification]] = []
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select f.*,
                       c.primary_class, c.direction, c.confidence, c.rule_version,
                       c.algorithm, c.secondary_tags, c.explanation, c.similar_days
                from research.banknifty_day_features f
                join research.banknifty_day_classifications c using (session_date)
                where f.session_date < %s
                order by f.session_date
                """,
                (before,),
            )
            for row in cur.fetchall():
                out.append((features_from_row(row), classification_from_row(row)))
    except Exception as exc:  # tables may not exist yet on first ever run
        conn.rollback()
        print(f"[warn] persisted library unavailable: {exc}", file=sys.stderr)
    return out


def upsert_session(conn, feats: BankNiftyDayFeatures, label: PatternClassification) -> None:
    frec = feature_record(feats)
    crec = classification_record(label)
    fcols = list(frec)
    ccols = list(crec)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            insert into research.banknifty_day_features ({', '.join(fcols)})
            values ({', '.join(['%s'] * len(fcols))})
            on conflict (session_date) do update set
                {', '.join(f'{c} = excluded.{c}' for c in fcols if c != 'session_date')},
                updated_at = now()
            """,
            [frec[c] for c in fcols],
        )
        cur.execute(
            f"""
            insert into research.banknifty_day_classifications ({', '.join(ccols)})
            values ({', '.join(['%s'] * len(ccols))})
            on conflict (session_date) do update set
                {', '.join(f'{c} = excluded.{c}' for c in ccols if c != 'session_date')},
                updated_at = now()
            """,
            [crec[c] for c in ccols],
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build BankNifty trend-pattern library (research/paper-only).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--from", dest="start", help="YYYY-MM-DD")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD")
    p.add_argument("--date", dest="single", help="single session YYYY-MM-DD (shortcut for --from/--to)")
    p.add_argument("--resolution", default=None, help="candle resolution (default from config)")
    p.add_argument("--limit", type=int, default=None, help="process at most N most-recent sessions")
    p.add_argument("--dry-run", action="store_true", help="do not write to the database")
    p.add_argument("--print", dest="do_print", action="store_true", help="print a per-day summary")
    return p.parse_args(argv)


def resolve_range(args: argparse.Namespace) -> tuple[date, date]:
    if args.single:
        d = date.fromisoformat(args.single)
        return d, d
    if not args.start or not args.end:
        raise SystemExit("Provide --date or both --from and --to")
    return date.fromisoformat(args.start), date.fromisoformat(args.end)


def summarize_line(feats: BankNiftyDayFeatures, label: PatternClassification) -> str:
    ret = feats.day_return_pct if feats.day_return_pct is not None else Decimal("0")
    return (
        f"{feats.session_date}  {label.primary_class:<14} {label.direction:<8} "
        f"conf={label.confidence}  ret={ret}%  crosses={feats.vwap_cross_count}  "
        f"similar={len(label.similar_days)}  oc={'Y' if feats.option_chain_available else 'n'}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_pattern_config(args.config)
    resolution = args.resolution or str(config.get("resolution", "5"))
    start, end = resolve_range(args)
    weights: dict[str, Decimal] = config.get("_constituent_weights", {})
    underlying = config.get("underlying", "BANKNIFTY")
    index_symbol = config.get("underlying_symbol", "NSE:NIFTYBANK-INDEX")
    symbols = [index_symbol, *weights.keys()]

    # Load enough prior candle history so single-date / short-window runs can still
    # compute prev_close (gap) and range_vs_adr10, then seed the similar-day search
    # from already-persisted library rows.
    fetch_start = effective_fetch_start(start, config)
    include_library = bool(config.get("library", {}).get("include_persisted_similar_days", True))

    conn = connect_db()
    candles = fetch_candles(conn, symbols, resolution, fetch_start, end)
    option_chain_by_day = fetch_option_chain_by_day(conn, underlying, fetch_start, end)
    prior_library = load_persisted_library(conn, start) if include_library else []

    index_by_day = group_by_ist_day(candles.get(index_symbol, []))
    constituent_by_day = {sym: group_by_ist_day(candles.get(sym, [])) for sym in weights}

    built = analyze_sessions(
        config=config,
        index_by_day=index_by_day,
        constituent_by_day=constituent_by_day,
        weights=weights,
        option_chain_by_day=option_chain_by_day,
        prior_library=prior_library,
    )

    # Only persist/report the requested range; the earlier history days were loaded
    # purely as feature/similar-day context.
    in_range = [(f, l) for f, l in built if start <= date.fromisoformat(f.session_date) <= end]
    if args.limit is not None:
        in_range = in_range[-args.limit:]

    written = 0
    for feats, label in in_range:
        if feats.candle_count < int(config.get("session", {}).get("min_candles_for_classification", 0)):
            if args.do_print:
                print(f"{feats.session_date}  SKIPPED (only {feats.candle_count} candles)")
            continue
        if args.do_print:
            print(summarize_line(feats, label))
        if not args.dry_run:
            upsert_session(conn, feats, label)
            written += 1
    if not args.dry_run:
        conn.commit()
    conn.close()

    print(json.dumps({
        "from": start.isoformat(), "to": end.isoformat(), "resolution": resolution,
        "history_from": fetch_start.isoformat(),
        "sessions_analyzed": len(in_range),
        "history_sessions": len(built) - len(in_range),
        "library_rows": len(prior_library),
        "written": written, "dry_run": args.dry_run,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
