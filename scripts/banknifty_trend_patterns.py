#!/usr/bin/env python3
"""Pure feature-extraction + deterministic classification for the BankNifty
daywise trend-pattern library.

Research / paper-only analytics. This module contains **no DB writes** and **no
FYERS order calls** — only pure dataclasses and pure functions over candle
inputs. Persistence and reporting live in the sibling scripts:

  * scripts/build_banknifty_trend_pattern_library.py  (persists features+labels)
  * scripts/generate_banknifty_trend_pattern_report.py (after-market report)

Every session is classified into exactly one primary class:

    trend | range | spike_channel | trending_range | reversal | chop

Money / percent math uses Decimal (ROUND_HALF_UP). Intraday time logic is
IST-aware (ZoneInfo("Asia/Kolkata")). All thresholds are config-driven; nothing
is hardcoded in the classifier.

Exit model is runner-style and must never regress to a fixed profit cap:
after +0.5R the paper stop moves to breakeven + one tick / cost proxy, then the
trade trails via an MFE ratchet / structure trailing. See summarize_playbook().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

PCT = Decimal("0.01")          # quantum for percentage / location values
VOL_Q = Decimal("0.0001")      # quantum for realized-vol / similarity values
PRICE_Q = Decimal("0.01")      # quantum for price-derived levels

PRIMARY_CLASSES = (
    "trend",
    "range",
    "spike_channel",
    "trending_range",
    "reversal",
    "chop",
)
DIRECTIONS = ("bullish", "bearish", "neutral", "mixed")

# Ordered feature names used to build the nearest-neighbour vector. Weights for
# each come from config["nearest_neighbor"]["feature_weights"]; a missing weight
# defaults to 0 (feature ignored), so the config is the single source of truth.
NN_FEATURE_ORDER = (
    "gap_pct",
    "day_return_pct",
    "day_range_pct",
    "orb_range_pct",
    "close_location",
    "vwap_cross_count",
    "vwap_side_pct",
    "realized_vol",
    "range_vs_adr10",
    "weighted_positive_breadth_pct",
)


# --------------------------------------------------------------------------- #
# Config safety validation (shared by builder + report generator)
# --------------------------------------------------------------------------- #
def validate_pattern_config_safety(config: Mapping[str, Any]) -> None:
    """Reject unsafe trend-pattern configs. Mirrors the repo-wide safety rails:

    * ``paper_only`` must be the *boolean* ``True`` (a truthy string like "true"
      or 1 is rejected — config validation must not be fooled by string booleans).
    * ``live_orders_enabled`` must be the *boolean* ``False``.
    * The exit model must stay runner-style with NO fixed profit cap, enforced
      exactly for this strategy:

        - ``fixed_target_exit_enabled``  == boolean ``False``
        - ``profit_lock_trigger``        == 0          (profit-lock disabled)
        - ``profit_lock_step``           == 0          (profit-lock disabled)
        - ``breakeven_at_r``             == 0.5        (the 0.5R breakeven rule)
        - ``ratchet_start_r``            present and > 0
        - ``ratchet_giveback_pct``       present and > 0
        - ``ratchet_giveback_min_inr``   present and >= 0

      Booleans, strings, missing keys, and out-of-range values are all rejected —
      ``True``/``False`` are never accepted in place of a number (Python treats
      ``bool`` as a subclass of ``int``, so it is excluded explicitly).

    Raises ``ValueError`` on any violation (never silently downgrades safety).
    """
    if config.get("paper_only") is not True:
        raise ValueError("Refusing config: paper_only must be boolean true")
    if config.get("live_orders_enabled", False) is not False:
        raise ValueError("Refusing config: live_orders_enabled must be boolean false")

    exit_model = config.get("exit_model")
    if not isinstance(exit_model, Mapping):
        raise ValueError("Refusing config: exit_model block is required (runner-style exits)")

    _MISSING = object()

    def _number(key: str) -> float:
        """Return a strictly-numeric exit-model value, rejecting bool/str/missing."""
        value = exit_model.get(key, _MISSING)
        if value is _MISSING:
            raise ValueError(f"Refusing config: exit_model.{key} is required")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"Refusing config: exit_model.{key} must be a number "
                f"(got {type(value).__name__})"
            )
        return float(value)

    if exit_model.get("fixed_target_exit_enabled", _MISSING) is not False:
        raise ValueError(
            "Refusing config: exit_model.fixed_target_exit_enabled must be boolean "
            "false — runner-style exits only, never a fixed profit cap"
        )

    if _number("profit_lock_trigger") != 0:
        raise ValueError(
            "Refusing config: exit_model.profit_lock_trigger must be 0 "
            "(profit-lock disabled — no fixed profit cap)"
        )
    if _number("profit_lock_step") != 0:
        raise ValueError(
            "Refusing config: exit_model.profit_lock_step must be 0 "
            "(profit-lock disabled — no fixed profit cap)"
        )
    if _number("breakeven_at_r") != 0.5:
        raise ValueError(
            "Refusing config: exit_model.breakeven_at_r must be 0.5 "
            "(the 0.5R breakeven rule)"
        )
    if _number("ratchet_start_r") <= 0:
        raise ValueError(
            "Refusing config: exit_model.ratchet_start_r must be a positive number "
            "(MFE ratchet must arm above breakeven)"
        )
    if _number("ratchet_giveback_pct") <= 0:
        raise ValueError(
            "Refusing config: exit_model.ratchet_giveback_pct must be a positive number "
            "(MFE trailing giveback)"
        )
    if _number("ratchet_giveback_min_inr") < 0:
        raise ValueError(
            "Refusing config: exit_model.ratchet_giveback_min_inr must be >= 0 "
            "(MFE trailing giveback floor)"
        )


# --------------------------------------------------------------------------- #
# Candle + helpers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Candle:
    """A single OHLCV bar. ``ts`` may be tz-aware (preferred) or naive-IST."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0

    def ist_time(self) -> time:
        ts = self.ts
        if ts.tzinfo is not None:
            ts = ts.astimezone(IST)
        return ts.time()


def _d(value: Any) -> Decimal:
    """Coerce to Decimal via str (never via float) to avoid binary artefacts."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def to_candle(row: Mapping[str, Any] | Candle) -> Candle:
    if isinstance(row, Candle):
        return row
    return Candle(
        ts=row["ts"],
        open=_d(row["open"]),
        high=_d(row["high"]),
        low=_d(row["low"]),
        close=_d(row["close"]),
        volume=int(row.get("volume") or 0),
    )


def q_pct(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(PCT, rounding=ROUND_HALF_UP)


def _safe_div(num: Decimal, den: Decimal) -> Decimal | None:
    if den == 0:
        return None
    return num / den


def parse_ist_time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _within(t: time, start: time, end: time) -> bool:
    return start <= t < end


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class DaySegment:
    name: str
    start_ist: str
    end_ist: str
    return_pct: Decimal | None
    range_pct: Decimal | None
    vwap_side_pct: Decimal | None
    net_direction: str
    volume_share: Decimal | None
    close_location: Decimal | None
    candle_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start_ist": self.start_ist,
            "end_ist": self.end_ist,
            "return_pct": _num(self.return_pct),
            "range_pct": _num(self.range_pct),
            "vwap_side_pct": _num(self.vwap_side_pct),
            "net_direction": self.net_direction,
            "volume_share": _num(self.volume_share),
            "close_location": _num(self.close_location),
            "candle_count": self.candle_count,
        }


@dataclass
class BankNiftyDayFeatures:
    session_date: str
    underlying: str
    underlying_symbol: str
    resolution: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    prev_close: Decimal | None
    gap_pct: Decimal | None
    day_return_pct: Decimal | None
    day_range_pct: Decimal | None
    orb_high: Decimal | None
    orb_low: Decimal | None
    orb_range_pct: Decimal | None
    orb_break_direction: str
    orb_hold: bool
    close_location: Decimal | None
    vwap_cross_count: int
    vwap_side_pct: Decimal | None
    realized_vol: Decimal | None
    range_vs_adr10: Decimal | None
    mfe_from_open_pct: Decimal | None
    mae_from_open_pct: Decimal | None
    day_high_time: str | None
    day_low_time: str | None
    weighted_positive_breadth_pct: Decimal | None
    weighted_negative_breadth_pct: Decimal | None
    weighted_vwap_confirm_pct: Decimal | None
    breadth_divergence: bool
    top_positive_contributors: list[dict[str, Any]]
    top_negative_contributors: list[dict[str, Any]]
    atm_iv: Decimal | None
    iv_regime: str | None
    pcr: Decimal | None
    max_pain_distance_pct: Decimal | None
    option_chain_available: bool
    candle_count: int
    segments: list[DaySegment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_feature_dict(self) -> dict[str, Any]:
        """Compact serialisable view (the ``features`` jsonb payload)."""
        return {
            "open": _num(self.open),
            "high": _num(self.high),
            "low": _num(self.low),
            "close": _num(self.close),
            "prev_close": _num(self.prev_close),
            "gap_pct": _num(self.gap_pct),
            "day_return_pct": _num(self.day_return_pct),
            "day_range_pct": _num(self.day_range_pct),
            "orb_high": _num(self.orb_high),
            "orb_low": _num(self.orb_low),
            "orb_range_pct": _num(self.orb_range_pct),
            "orb_break_direction": self.orb_break_direction,
            "orb_hold": self.orb_hold,
            "close_location": _num(self.close_location),
            "vwap_cross_count": self.vwap_cross_count,
            "vwap_side_pct": _num(self.vwap_side_pct),
            "realized_vol": _num(self.realized_vol),
            "range_vs_adr10": _num(self.range_vs_adr10),
            "mfe_from_open_pct": _num(self.mfe_from_open_pct),
            "mae_from_open_pct": _num(self.mae_from_open_pct),
            "day_high_time": self.day_high_time,
            "day_low_time": self.day_low_time,
            "weighted_positive_breadth_pct": _num(self.weighted_positive_breadth_pct),
            "weighted_negative_breadth_pct": _num(self.weighted_negative_breadth_pct),
            "weighted_vwap_confirm_pct": _num(self.weighted_vwap_confirm_pct),
            "breadth_divergence": self.breadth_divergence,
            "top_positive_contributors": self.top_positive_contributors,
            "top_negative_contributors": self.top_negative_contributors,
            "atm_iv": _num(self.atm_iv),
            "iv_regime": self.iv_regime,
            "pcr": _num(self.pcr),
            "max_pain_distance_pct": _num(self.max_pain_distance_pct),
            "option_chain_available": self.option_chain_available,
            "candle_count": self.candle_count,
            "warnings": list(self.warnings),
        }


@dataclass
class PatternClassification:
    session_date: str
    primary_class: str
    direction: str
    confidence: Decimal
    rule_version: str
    algorithm: str
    secondary_tags: list[str] = field(default_factory=list)
    explanation: dict[str, Any] = field(default_factory=dict)
    similar_days: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SimilarDay:
    session_date: str
    primary_class: str
    direction: str
    distance: Decimal
    similarity: Decimal
    day_return_pct: Decimal | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date,
            "primary_class": self.primary_class,
            "direction": self.direction,
            "distance": _num(self.distance),
            "similarity": _num(self.similarity),
            "day_return_pct": _num(self.day_return_pct),
            "note": self.note,
        }


def _num(value: Decimal | None) -> float | None:
    """Decimal -> float for JSON payloads (storage keeps numeric precision)."""
    if value is None:
        return None
    return float(value)


# --------------------------------------------------------------------------- #
# Intraday primitives
# --------------------------------------------------------------------------- #
def typical_price(c: Candle) -> Decimal:
    return (c.high + c.low + c.close) / Decimal("3")


def cumulative_vwap_series(candles: Sequence[Candle]) -> list[Decimal]:
    """Cumulative VWAP after each candle.

    Uses volume-weighted typical price when volume is present; falls back to an
    unweighted running mean of typical prices when the series carries no volume
    (NSE index candles frequently report 0 volume). The fallback is recorded as
    a warning by build_day_features so the classifier stays honest.
    """
    out: list[Decimal] = []
    have_volume = any(c.volume > 0 for c in candles)
    if have_volume:
        pv = Decimal("0")
        vol = Decimal("0")
        for c in candles:
            v = Decimal(c.volume if c.volume > 0 else 0)
            pv += typical_price(c) * v
            vol += v
            out.append(pv / vol if vol > 0 else typical_price(c))
        return out
    tp_sum = Decimal("0")
    for i, c in enumerate(candles, start=1):
        tp_sum += typical_price(c)
        out.append(tp_sum / Decimal(i))
    return out


def vwap_cross_count(candles: Sequence[Candle], vwap: Sequence[Decimal], min_distance_pct: Decimal) -> int:
    """Count sign changes of (close - vwap), ignoring crosses inside a small
    neutral band (``min_distance_pct`` of vwap) to avoid counting noise."""
    crosses = 0
    last_side = 0
    for c, vw in zip(candles, vwap):
        if vw == 0:
            continue
        diff_pct = (c.close - vw) / vw * Decimal("100")
        if diff_pct > min_distance_pct:
            side = 1
        elif diff_pct < -min_distance_pct:
            side = -1
        else:
            continue
        if last_side != 0 and side != last_side:
            crosses += 1
        last_side = side
    return crosses


def vwap_side_pct(candles: Sequence[Candle], vwap: Sequence[Decimal]) -> Decimal | None:
    """Share (%) of candles closing at-or-above cumulative VWAP."""
    if not candles:
        return None
    above = sum(1 for c, vw in zip(candles, vwap) if c.close >= vw)
    return (Decimal(above) / Decimal(len(candles)) * Decimal("100")).quantize(PCT, rounding=ROUND_HALF_UP)


def close_location(high: Decimal, low: Decimal, close: Decimal) -> Decimal | None:
    rng = high - low
    if rng == 0:
        return None
    return ((close - low) / rng).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def realized_vol_5m(candles: Sequence[Candle]) -> Decimal | None:
    """Standard deviation (%) of consecutive 5m close-to-close returns."""
    closes = [c.close for c in candles if c.close > 0]
    if len(closes) < 3:
        return None
    rets: list[Decimal] = []
    for prev, cur in zip(closes, closes[1:]):
        r = _safe_div((cur - prev), prev)
        if r is not None:
            rets.append(r * Decimal("100"))
    if len(rets) < 2:
        return None
    mean = sum(rets, Decimal("0")) / Decimal(len(rets))
    var = sum(((r - mean) ** 2 for r in rets), Decimal("0")) / Decimal(len(rets))
    return var.sqrt().quantize(VOL_Q, rounding=ROUND_HALF_UP)


def _segment_candles(candles: Sequence[Candle], start: time, end: time) -> list[Candle]:
    return [c for c in candles if _within(c.ist_time(), start, end)]


def build_segments(
    candles: Sequence[Candle],
    vwap: Sequence[Decimal],
    seg_specs: Sequence[Mapping[str, str]],
    total_volume: Decimal,
) -> list[DaySegment]:
    segments: list[DaySegment] = []
    vwap_by_ts = {c.ts: vw for c, vw in zip(candles, vwap)}
    for spec in seg_specs:
        start = parse_ist_time(spec["start_ist"])
        end = parse_ist_time(spec["end_ist"])
        seg = _segment_candles(candles, start, end)
        if not seg:
            segments.append(
                DaySegment(
                    name=spec["name"], start_ist=spec["start_ist"], end_ist=spec["end_ist"],
                    return_pct=None, range_pct=None, vwap_side_pct=None,
                    net_direction="flat", volume_share=None, close_location=None, candle_count=0,
                )
            )
            continue
        s_open = seg[0].open
        s_close = seg[-1].close
        s_high = max(c.high for c in seg)
        s_low = min(c.low for c in seg)
        ret = q_pct((s_close - s_open) / s_open * Decimal("100")) if s_open else None
        rng = q_pct((s_high - s_low) / s_open * Decimal("100")) if s_open else None
        above = sum(1 for c in seg if vwap_by_ts.get(c.ts) is not None and c.close >= vwap_by_ts[c.ts])
        side = (Decimal(above) / Decimal(len(seg)) * Decimal("100")).quantize(PCT, rounding=ROUND_HALF_UP)
        seg_vol = sum(Decimal(c.volume) for c in seg)
        vol_share = (seg_vol / total_volume * Decimal("100")).quantize(PCT, rounding=ROUND_HALF_UP) if total_volume > 0 else None
        net = "up" if s_close > s_open else "down" if s_close < s_open else "flat"
        segments.append(
            DaySegment(
                name=spec["name"], start_ist=spec["start_ist"], end_ist=spec["end_ist"],
                return_pct=ret, range_pct=rng, vwap_side_pct=side, net_direction=net,
                volume_share=vol_share, close_location=close_location(s_high, s_low, s_close),
                candle_count=len(seg),
            )
        )
    return segments


def _adr10(prior_days: Sequence[Sequence[Candle]], lookback: int) -> Decimal | None:
    vals: list[Decimal] = []
    for rows in list(prior_days)[-lookback:]:
        if rows:
            vals.append(max(c.high for c in rows) - min(c.low for c in rows))
    if not vals:
        return None
    return sum(vals, Decimal("0")) / Decimal(len(vals))


# --------------------------------------------------------------------------- #
# Breadth
# --------------------------------------------------------------------------- #
def compute_breadth(
    constituent_candles: Mapping[str, Sequence[Candle]],
    weights: Mapping[str, Decimal],
    index_direction: str,
    top_n: int,
) -> dict[str, Any]:
    """Weighted constituent breadth for the day.

    Returns weighted positive/negative breadth %, weighted VWAP-confirmation %
    (share of weight whose close sits on the index-direction side of its own
    VWAP), and the top positive/negative contributors by weight*move.
    """
    covered = Decimal("0")
    pos_w = Decimal("0")
    neg_w = Decimal("0")
    confirm_w = Decimal("0")
    moves: list[tuple[Decimal, str, Decimal]] = []  # (weight*move, symbol, move_pct)
    for symbol, rows in constituent_candles.items():
        w = _d(weights.get(symbol, 0))
        if w <= 0 or not rows:
            continue
        rows = sorted(rows, key=lambda c: c.ts)
        open_ = rows[0].open
        close = rows[-1].close
        if open_ <= 0:
            continue
        covered += w
        move = (close - open_) / open_ * Decimal("100")
        if move > 0:
            pos_w += w
        elif move < 0:
            neg_w += w
        vw = cumulative_vwap_series(rows)[-1]
        on_dir_side = (
            (index_direction == "bullish" and close >= vw)
            or (index_direction == "bearish" and close <= vw)
        )
        if on_dir_side:
            confirm_w += w
        moves.append((w * move, symbol, move.quantize(PCT, rounding=ROUND_HALF_UP)))
    if covered <= 0:
        return {
            "weighted_positive_breadth_pct": None,
            "weighted_negative_breadth_pct": None,
            "weighted_vwap_confirm_pct": None,
            "top_positive_contributors": [],
            "top_negative_contributors": [],
            "coverage_pct": None,
        }
    pos_pct = (pos_w / covered * Decimal("100")).quantize(PCT, rounding=ROUND_HALF_UP)
    neg_pct = (neg_w / covered * Decimal("100")).quantize(PCT, rounding=ROUND_HALF_UP)
    confirm_pct = (confirm_w / covered * Decimal("100")).quantize(PCT, rounding=ROUND_HALF_UP)
    moves.sort(key=lambda m: m[0], reverse=True)
    top_pos = [
        {"symbol": s, "contribution": float(c.quantize(VOL_Q, rounding=ROUND_HALF_UP)), "move_pct": float(mv)}
        for c, s, mv in moves[:top_n] if c > 0
    ]
    top_neg = [
        {"symbol": s, "contribution": float(c.quantize(VOL_Q, rounding=ROUND_HALF_UP)), "move_pct": float(mv)}
        for c, s, mv in sorted(moves, key=lambda m: m[0])[:top_n] if c < 0
    ]
    return {
        "weighted_positive_breadth_pct": pos_pct,
        "weighted_negative_breadth_pct": neg_pct,
        "weighted_vwap_confirm_pct": confirm_pct,
        "top_positive_contributors": top_pos,
        "top_negative_contributors": top_neg,
        "coverage_pct": covered.quantize(PCT, rounding=ROUND_HALF_UP),
    }


# --------------------------------------------------------------------------- #
# Feature builder
# --------------------------------------------------------------------------- #
def build_day_features(
    *,
    session_date: str,
    candles: Sequence[Mapping[str, Any] | Candle],
    config: Mapping[str, Any],
    prev_close: Any = None,
    prior_days_candles: Sequence[Sequence[Mapping[str, Any] | Candle]] | None = None,
    constituent_candles: Mapping[str, Sequence[Mapping[str, Any] | Candle]] | None = None,
    weights: Mapping[str, Any] | None = None,
    option_chain: Mapping[str, Any] | None = None,
) -> BankNiftyDayFeatures:
    """Pure feature extraction for a single session. No DB / network access."""
    rows = sorted((to_candle(c) for c in candles), key=lambda c: c.ts)
    warnings: list[str] = []
    underlying = config.get("underlying", "BANKNIFTY")
    underlying_symbol = config.get("underlying_symbol", "NSE:NIFTYBANK-INDEX")
    resolution = str(config.get("resolution", "5"))

    if not rows:
        warnings.append("no candles for session")
        return _empty_features(session_date, underlying, underlying_symbol, resolution, warnings)

    if not any(c.volume > 0 for c in rows):
        warnings.append("no candle volume; VWAP uses unweighted typical price")

    o = rows[0].open
    h = max(c.high for c in rows)
    l = min(c.low for c in rows)
    c_last = rows[-1].close
    prev_close_d = _d(prev_close) if prev_close is not None else None

    day_return_pct = q_pct((c_last - o) / o * Decimal("100")) if o else None
    day_range_pct = q_pct((h - l) / o * Decimal("100")) if o else None
    gap_pct = q_pct((o - prev_close_d) / prev_close_d * Decimal("100")) if prev_close_d else None
    if prev_close_d is None:
        warnings.append("no prior close; gap_pct unavailable")

    # ORB
    orb_cfg = config.get("orb", {})
    orb_minutes = int(orb_cfg.get("window_minutes", 15))
    orb_buffer = _d(orb_cfg.get("break_buffer_pct", 0))
    open_t = rows[0].ist_time()
    orb_cutoff = _add_minutes(open_t, orb_minutes)
    orb_rows = [c for c in rows if c.ist_time() < orb_cutoff]
    if orb_rows:
        orb_high = max(c.high for c in orb_rows)
        orb_low = min(c.low for c in orb_rows)
    else:
        orb_high = orb_low = None
    orb_range_pct = q_pct((orb_high - orb_low) / o * Decimal("100")) if (orb_high is not None and o) else None
    orb_break_direction = "none"
    orb_hold = False
    if orb_high is not None and orb_low is not None:
        up_level = orb_high * (Decimal("1") + orb_buffer / Decimal("100"))
        down_level = orb_low * (Decimal("1") - orb_buffer / Decimal("100"))
        broke_up = any(c.close > up_level for c in rows if c.ist_time() >= orb_cutoff)
        broke_down = any(c.close < down_level for c in rows if c.ist_time() >= orb_cutoff)
        if broke_up and not broke_down:
            orb_break_direction = "up"
            orb_hold = c_last > orb_high
        elif broke_down and not broke_up:
            orb_break_direction = "down"
            orb_hold = c_last < orb_low
        elif broke_up and broke_down:
            orb_break_direction = "both"
            orb_hold = c_last > orb_high or c_last < orb_low

    # VWAP
    vwap = cumulative_vwap_series(rows)
    vwap_cfg = config.get("vwap", {})
    x_count = vwap_cross_count(rows, vwap, _d(vwap_cfg.get("cross_min_distance_pct", 0)))
    side_pct = vwap_side_pct(rows, vwap)

    cl = close_location(h, l, c_last)
    rvol = realized_vol_5m(rows)

    # MFE / MAE from open
    mfe = q_pct((h - o) / o * Decimal("100")) if o else None
    mae = q_pct((l - o) / o * Decimal("100")) if o else None
    day_high_time = _fmt_time(max(rows, key=lambda c: c.high).ist_time())
    day_low_time = _fmt_time(min(rows, key=lambda c: c.low).ist_time())

    # ADR / range_vs_adr10
    rv_cfg = config.get("realized_vol", {})
    adr_lookback = int(rv_cfg.get("adr_lookback_days", 10))
    range_vs_adr10 = None
    if prior_days_candles:
        prior_sorted = [sorted((to_candle(c) for c in day), key=lambda c: c.ts) for day in prior_days_candles]
        adr = _adr10(prior_sorted, adr_lookback)
        if adr and adr > 0:
            range_vs_adr10 = ((h - l) / adr).quantize(VOL_Q, rounding=ROUND_HALF_UP)

    # Direction (used by breadth confirmation + segments)
    cls_cfg = config.get("classification", {})
    dir_thr = _d(cls_cfg.get("direction_min_return_pct", 0))
    index_direction = _direction_from_return(day_return_pct, dir_thr)

    # Segments
    total_volume = sum(Decimal(c.volume) for c in rows)
    segments = build_segments(rows, vwap, config.get("segments", []), total_volume)

    # Breadth
    breadth_cfg = config.get("breadth", {})
    top_n = int(breadth_cfg.get("top_contributors", 3))
    breadth = {
        "weighted_positive_breadth_pct": None,
        "weighted_negative_breadth_pct": None,
        "weighted_vwap_confirm_pct": None,
        "top_positive_contributors": [],
        "top_negative_contributors": [],
    }
    breadth_divergence = False
    if constituent_candles and weights:
        cc = {sym: [to_candle(c) for c in rows_] for sym, rows_ in constituent_candles.items()}
        wmap = {sym: _d(w) for sym, w in weights.items()}
        breadth = compute_breadth(cc, wmap, index_direction, top_n)
        div_pct = _d(breadth_cfg.get("divergence_pct", 55))
        pos = breadth.get("weighted_positive_breadth_pct")
        neg = breadth.get("weighted_negative_breadth_pct")
        if index_direction == "bullish" and neg is not None and neg >= div_pct:
            breadth_divergence = True
        elif index_direction == "bearish" and pos is not None and pos >= div_pct:
            breadth_divergence = True
    else:
        warnings.append("no constituent breadth inputs; breadth metrics unavailable")

    # Option chain (optional; missing data must not fail classification)
    oc_available = bool(option_chain)
    atm_iv = iv_regime = pcr = max_pain_distance_pct = None
    if option_chain:
        atm_iv = _opt_decimal(option_chain.get("atm_iv"))
        iv_regime = option_chain.get("iv_regime")
        pcr = _opt_decimal(option_chain.get("pcr"))
        mp = _opt_decimal(option_chain.get("max_pain_strike"))
        spot = _opt_decimal(option_chain.get("spot")) or c_last
        if mp is not None and spot and spot > 0:
            max_pain_distance_pct = q_pct((spot - mp) / spot * Decimal("100"))
    else:
        warnings.append("option-chain context unavailable for session")

    return BankNiftyDayFeatures(
        session_date=session_date,
        underlying=underlying,
        underlying_symbol=underlying_symbol,
        resolution=resolution,
        open=o, high=h, low=l, close=c_last, prev_close=prev_close_d,
        gap_pct=gap_pct, day_return_pct=day_return_pct, day_range_pct=day_range_pct,
        orb_high=orb_high, orb_low=orb_low, orb_range_pct=orb_range_pct,
        orb_break_direction=orb_break_direction, orb_hold=orb_hold,
        close_location=cl, vwap_cross_count=x_count, vwap_side_pct=side_pct,
        realized_vol=rvol, range_vs_adr10=range_vs_adr10,
        mfe_from_open_pct=mfe, mae_from_open_pct=mae,
        day_high_time=day_high_time, day_low_time=day_low_time,
        weighted_positive_breadth_pct=breadth["weighted_positive_breadth_pct"],
        weighted_negative_breadth_pct=breadth["weighted_negative_breadth_pct"],
        weighted_vwap_confirm_pct=breadth["weighted_vwap_confirm_pct"],
        breadth_divergence=breadth_divergence,
        top_positive_contributors=breadth["top_positive_contributors"],
        top_negative_contributors=breadth["top_negative_contributors"],
        atm_iv=atm_iv, iv_regime=iv_regime, pcr=pcr,
        max_pain_distance_pct=max_pain_distance_pct,
        option_chain_available=oc_available,
        candle_count=len(rows),
        segments=segments,
        warnings=warnings,
    )


def _empty_features(session_date, underlying, underlying_symbol, resolution, warnings) -> BankNiftyDayFeatures:
    return BankNiftyDayFeatures(
        session_date=session_date, underlying=underlying, underlying_symbol=underlying_symbol,
        resolution=resolution, open=None, high=None, low=None, close=None, prev_close=None,
        gap_pct=None, day_return_pct=None, day_range_pct=None, orb_high=None, orb_low=None,
        orb_range_pct=None, orb_break_direction="none", orb_hold=False, close_location=None,
        vwap_cross_count=0, vwap_side_pct=None, realized_vol=None, range_vs_adr10=None,
        mfe_from_open_pct=None, mae_from_open_pct=None, day_high_time=None, day_low_time=None,
        weighted_positive_breadth_pct=None, weighted_negative_breadth_pct=None,
        weighted_vwap_confirm_pct=None, breadth_divergence=False,
        top_positive_contributors=[], top_negative_contributors=[],
        atm_iv=None, iv_regime=None, pcr=None, max_pain_distance_pct=None,
        option_chain_available=False, candle_count=0, segments=[], warnings=warnings,
    )


def _opt_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return _d(value)
    except Exception:
        return None


def _direction_from_return(day_return_pct: Decimal | None, threshold: Decimal) -> str:
    if day_return_pct is None:
        return "neutral"
    if day_return_pct >= threshold:
        return "bullish"
    if day_return_pct <= -threshold:
        return "bearish"
    return "neutral"


def _add_minutes(t: time, minutes: int) -> time:
    total = t.hour * 60 + t.minute + minutes
    return time((total // 60) % 24, total % 60)


def _fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


# --------------------------------------------------------------------------- #
# Classification (deterministic rules v1)
# --------------------------------------------------------------------------- #
def classify_day_rules(features: BankNiftyDayFeatures, config: Mapping[str, Any]) -> PatternClassification:
    """Interpretable, config-driven classifier. Scores each candidate class from
    the day features and selects the highest-scoring class. No ML."""
    cls = config.get("classification", {})
    rule_version = config.get("rule_version", "banknifty_trend_patterns_v1")
    algorithm = config.get("algorithm", "deterministic_rules")
    session_date = features.session_date

    if features.candle_count < int(config.get("session", {}).get("min_candles_for_classification", 0)) \
            or features.day_return_pct is None or features.close_location is None:
        return PatternClassification(
            session_date=session_date, primary_class="chop", direction="neutral",
            confidence=Decimal("0.0000"), rule_version=rule_version, algorithm=algorithm,
            secondary_tags=["insufficient_data"],
            explanation={"reason": "insufficient candles/features for classification",
                         "candle_count": features.candle_count},
        )

    ret = features.day_return_pct
    abs_ret = abs(ret)
    cl_loc = features.close_location
    crosses = features.vwap_cross_count
    side = features.vwap_side_pct if features.vwap_side_pct is not None else Decimal("50")
    dir_thr = _d(cls.get("direction_min_return_pct", 0))
    direction = _direction_from_return(ret, dir_thr)

    # breadth confirmation. When constituent breadth is unavailable the gate must
    # not veto a classification (missing data is warned, never guessed): we treat
    # the breadth check as satisfied and fall back to a neutral 50 for scoring.
    breadth_available = features.weighted_positive_breadth_pct is not None
    if direction == "bullish":
        breadth_confirm = features.weighted_positive_breadth_pct
    elif direction == "bearish":
        breadth_confirm = features.weighted_negative_breadth_pct
    else:
        breadth_confirm = None
    breadth_confirm = breadth_confirm if breadth_confirm is not None else Decimal("50")

    open_drive = next((s for s in features.segments if s.name == "open_drive"), None)
    od_ret = open_drive.return_pct if open_drive and open_drive.return_pct is not None else Decimal("0")

    evidence: dict[str, Any] = {
        "day_return_pct": _num(ret),
        "close_location": _num(cl_loc),
        "vwap_cross_count": crosses,
        "vwap_side_pct": _num(side),
        "breadth_confirm_pct": _num(breadth_confirm),
        "open_drive_return_pct": _num(od_ret),
        "orb_break_direction": features.orb_break_direction,
        "orb_hold": features.orb_hold,
    }

    scores: dict[str, Decimal] = {}

    # --- trend ---
    trend_min = _d(cls.get("trend_min_abs_return_pct", 0))
    trend_max_x = int(cls.get("trend_max_vwap_crosses", 99))
    trend_cl = _d(cls.get("trend_min_close_location", 0))
    trend_breadth = _d(cls.get("trend_min_breadth_confirm_pct", 0))
    cl_extreme = cl_loc if direction != "bearish" else (Decimal("1") - cl_loc)
    breadth_ok = (breadth_confirm >= trend_breadth) or not breadth_available
    if abs_ret >= trend_min and crosses <= trend_max_x and cl_extreme >= trend_cl \
            and breadth_ok and direction in ("bullish", "bearish"):
        scores["trend"] = (
            (abs_ret / trend_min)
            + cl_extreme
            + (breadth_confirm / Decimal("100"))
            + Decimal(str(max(0, trend_max_x - crosses))) / Decimal("10")
        )

    # --- spike_channel ---
    sc_od = _d(cls.get("spike_channel_min_open_drive_return_pct", 0))
    sc_pull = _d(cls.get("spike_channel_max_pullback_ratio", 1))
    pullback_ratio = _pullback_ratio(features)
    if abs(od_ret) >= sc_od and abs_ret >= trend_min and direction in ("bullish", "bearish") \
            and pullback_ratio is not None and pullback_ratio <= sc_pull:
        scores["spike_channel"] = (
            (abs(od_ret) / sc_od)
            + (Decimal("1") - pullback_ratio)
            + cl_extreme
        )

    # --- trending_range ---
    tr_min = _d(cls.get("trending_range_min_abs_return_pct", 0))
    tr_side = _d(cls.get("trending_range_min_vwap_side_pct", 0))
    one_sided = max(side, Decimal("100") - side)
    if abs_ret >= tr_min and one_sided >= tr_side and direction in ("bullish", "bearish"):
        scores["trending_range"] = (one_sided / Decimal("100")) + (abs_ret / tr_min) / Decimal("2")

    # --- range ---
    rng_max = _d(cls.get("range_max_abs_return_pct", 0))
    rng_min_x = int(cls.get("range_min_vwap_crosses", 0))
    rng_cl_lo = _d(cls.get("range_close_location_low", 0))
    rng_cl_hi = _d(cls.get("range_close_location_high", 1))
    if abs_ret <= rng_max and crosses >= rng_min_x and rng_cl_lo <= cl_loc <= rng_cl_hi:
        scores["range"] = (
            Decimal(str(crosses)) / Decimal("5")
            + (rng_max - abs_ret) / (rng_max if rng_max > 0 else Decimal("1"))
            + Decimal("1")
        )

    # --- reversal ---
    rev_od = _d(cls.get("reversal_min_open_drive_return_pct", 0))
    rev_rev = _d(cls.get("reversal_min_reverse_return_pct", 0))
    reversed_day = (
        (od_ret >= rev_od and ret <= -rev_rev and cl_loc <= _d(cls.get("range_close_location_low", 0)))
        or (od_ret <= -rev_od and ret >= rev_rev and cl_loc >= _d(cls.get("range_close_location_high", 1)))
    )
    if reversed_day:
        scores["reversal"] = (abs(od_ret) / rev_od if rev_od > 0 else Decimal("1")) + (abs_ret / rev_rev if rev_rev > 0 else Decimal("1"))

    # --- chop ---
    chop_max = _d(cls.get("chop_max_abs_return_pct", 0))
    chop_min_x = int(cls.get("chop_min_vwap_crosses", 0))
    chop_breadth = _d(cls.get("chop_max_breadth_confirm_pct", 100))
    if abs_ret <= chop_max and crosses >= chop_min_x:
        chop_score = Decimal(str(crosses)) / Decimal("5") + (chop_max - abs_ret) / (chop_max if chop_max > 0 else Decimal("1"))
        if breadth_confirm <= chop_breadth:
            chop_score += Decimal("0.5")
        scores["chop"] = chop_score

    # conflicting segment slopes nudge toward chop too
    seg_dirs = {s.net_direction for s in features.segments if s.candle_count > 0}
    if {"up", "down"} <= seg_dirs and abs_ret <= rng_max:
        scores["chop"] = scores.get("chop", Decimal("0")) + Decimal("0.4")

    if not scores:
        # Default: a directional-but-unremarkable day is a (weak) trending_range,
        # otherwise range. Guarantees exactly one primary class for every session.
        primary = "trending_range" if direction in ("bullish", "bearish") else "range"
        confidence = Decimal("0.3500")
        secondary = ["fallback_no_rule_match"]
        evidence["reason"] = "no rule scored; assigned fallback class"
    else:
        # Priority order resolves ties deterministically.
        priority = {"trend": 6, "spike_channel": 5, "reversal": 4, "trending_range": 3, "range": 2, "chop": 1}
        primary = max(scores, key=lambda k: (scores[k], priority[k]))
        confidence = _confidence(scores, primary)
        secondary = sorted(
            (k for k in scores if k != primary and scores[k] >= scores[primary] * Decimal("0.7")),
            key=lambda k: scores[k], reverse=True,
        )

    if features.breadth_divergence:
        secondary = list(dict.fromkeys([*secondary, "breadth_divergence"]))
    if features.orb_break_direction in ("up", "down") and not features.orb_hold:
        secondary = list(dict.fromkeys([*secondary, "orb_break_failed"]))

    evidence["scores"] = {k: float(v.quantize(VOL_Q, rounding=ROUND_HALF_UP)) for k, v in scores.items()}

    # Reversal / chop with weak directional return read as neutral/mixed.
    if primary in ("reversal",):
        direction = "bearish" if ret < 0 else "bullish"
    if primary == "chop" and abs_ret < dir_thr:
        direction = "mixed" if {"up", "down"} <= seg_dirs else "neutral"

    return PatternClassification(
        session_date=session_date, primary_class=primary, direction=direction,
        confidence=confidence, rule_version=rule_version, algorithm=algorithm,
        secondary_tags=secondary, explanation=evidence,
    )


def _pullback_ratio(features: BankNiftyDayFeatures) -> Decimal | None:
    """Adverse excursion against the day direction relative to favourable
    excursion — small ratio == shallow pullbacks (spike/channel behaviour)."""
    if features.mfe_from_open_pct is None or features.mae_from_open_pct is None:
        return None
    if (features.day_return_pct or Decimal("0")) >= 0:
        favourable = features.mfe_from_open_pct
        adverse = abs(features.mae_from_open_pct)
    else:
        favourable = abs(features.mae_from_open_pct)
        adverse = features.mfe_from_open_pct
    if favourable is None or favourable <= 0:
        return None
    return (adverse / favourable).quantize(VOL_Q, rounding=ROUND_HALF_UP)


def _confidence(scores: Mapping[str, Decimal], primary: str) -> Decimal:
    total = sum(scores.values(), Decimal("0"))
    if total <= 0:
        return Decimal("0.3500")
    share = scores[primary] / total
    # Map share (0..1) into a 0.35..0.95 confidence band so a single dominant
    # class is confident while a near-tie stays cautious.
    conf = Decimal("0.35") + share * Decimal("0.60")
    return conf.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Nearest-neighbour similar-day library
# --------------------------------------------------------------------------- #
def feature_vector(features: BankNiftyDayFeatures, config: Mapping[str, Any]) -> list[Decimal]:
    """Weighted feature vector for similarity. Missing values map to 0 so that a
    day with sparse data is simply less similar, never an error."""
    weights = config.get("nearest_neighbor", {}).get("feature_weights", {})
    vec: list[Decimal] = []
    raw = features.to_feature_dict()
    for name in NN_FEATURE_ORDER:
        w = _d(weights.get(name, 0))
        val = raw.get(name)
        d = _d(val) if val is not None else Decimal("0")
        vec.append((d * w).quantize(VOL_Q, rounding=ROUND_HALF_UP))
    return vec


def _euclidean(a: Sequence[Decimal], b: Sequence[Decimal]) -> Decimal:
    return (sum(((x - y) ** 2 for x, y in zip(a, b)), Decimal("0"))).sqrt()


def find_nearest_similar_days(
    target: BankNiftyDayFeatures,
    library: Iterable[tuple[BankNiftyDayFeatures, PatternClassification]],
    config: Mapping[str, Any],
    top_k: int | None = None,
) -> list[SimilarDay]:
    """Return the ``top_k`` most similar past sessions to ``target`` by weighted
    Euclidean distance over feature vectors. The target date is excluded."""
    nn_cfg = config.get("nearest_neighbor", {})
    if top_k is None:
        top_k = int(nn_cfg.get("top_k", 5))
    tvec = feature_vector(target, config)
    scored: list[SimilarDay] = []
    for feats, label in library:
        if feats.session_date == target.session_date:
            continue
        if feats.day_return_pct is None:
            continue
        dist = _euclidean(tvec, feature_vector(feats, config)).quantize(VOL_Q, rounding=ROUND_HALF_UP)
        similarity = (Decimal("1") / (Decimal("1") + dist)).quantize(VOL_Q, rounding=ROUND_HALF_UP)
        note = f"{label.primary_class}/{label.direction}, ret {_num(feats.day_return_pct)}%"
        scored.append(
            SimilarDay(
                session_date=feats.session_date, primary_class=label.primary_class,
                direction=label.direction, distance=dist, similarity=similarity,
                day_return_pct=feats.day_return_pct, note=note,
            )
        )
    scored.sort(key=lambda s: (s.distance, s.session_date))
    return scored[:top_k]


# --------------------------------------------------------------------------- #
# Playbook narrative (paper/research only; runner-style exits)
# --------------------------------------------------------------------------- #
EXIT_MODEL_SENTENCE = (
    "Exit model: after +0.5R move the paper stop to breakeven + one tick / cost "
    "proxy, then trail via MFE ratchet / structure trailing. No fixed profit cap."
)


def summarize_playbook(
    features: BankNiftyDayFeatures,
    classification: PatternClassification,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """How the session could have been played in paper/research terms, plus bot
    lessons. Always paper/research wording. The exit model is runner-style
    (0.5R breakeven + MFE trailing/ratchet) — never a fixed profit cap."""
    pc = classification.primary_class
    direction = classification.direction
    side = "CE (long calls)" if direction == "bullish" else "PE (long puts)" if direction == "bearish" else "no clear directional bias"

    how: list[str] = []
    if pc == "trend":
        how.append(f"Trend day ({direction}): paper-bias toward {side}. ORB hold + VWAP/pullback continuation entries; hold the runner.")
        how.append("Add only on shallow pullbacks that respect VWAP; avoid counter-trend fades.")
    elif pc == "spike_channel":
        how.append(f"Spike/channel ({direction}): early impulse with shallow pullbacks; paper-bias {side} on the first VWAP/structure pullback, then let it run in the channel.")
    elif pc == "trending_range":
        how.append(f"Trending range ({direction}): directional bias but rotational; be patient, take {side} only on confirmed reclaims of the working side of VWAP.")
    elif pc == "range":
        how.append("Range day: avoid breakout chasing. Only a defined range play (fade tested extremes back toward VWAP) if the range edge is clearly tested and rejected.")
    elif pc == "reversal":
        how.append(f"Reversal day: the early directional attempt failed; the played edge is the failed ORB / VWAP reclaim-or-reject in the {direction} direction, not the open drive.")
    else:  # chop
        how.append("Chop day: low conviction, frequent VWAP crosses. Best paper action is to stand aside; the no-chase guard should block most signals.")

    lessons: list[str] = []
    if features.orb_break_direction in ("up", "down"):
        held = "held" if features.orb_hold else "failed"
        lessons.append(f"ORB break {features.orb_break_direction} {held}; this is a primary allowed/blocked-entry signal for the day type.")
    if features.breadth_divergence:
        lessons.append("Breadth diverged from index direction — a no-chase / reduce-conviction observation.")
    if "orb_break_failed" in classification.secondary_tags:
        lessons.append("ORB break failed to hold — blocked-entry lesson for continuation chasers.")
    if not features.option_chain_available:
        lessons.append("Option-chain context (ATM IV / PCR / max-pain) was unavailable; classification used index + breadth only (warned, not guessed).")
    lessons.append(EXIT_MODEL_SENTENCE)

    return {
        "primary_class": pc,
        "direction": direction,
        "how_it_could_have_been_played": how,
        "bot_lessons": lessons,
        "exit_model": EXIT_MODEL_SENTENCE,
        "paper_only": True,
    }
