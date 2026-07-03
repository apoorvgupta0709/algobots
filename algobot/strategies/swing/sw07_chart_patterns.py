"""4.7 Classical Chart Patterns — bull flag & cup-and-handle continuation breakouts.

Flags, cup-and-handle, head-and-shoulders: structured supply-demand footprints
with measurable targets. This strategy trades the two most mechanical
continuation patterns LONG-only, on breakout with volume, with measured-move
targets and stops inside the pattern:

- BULL FLAG: a strong impulse pole followed by a tight, flat-to-down flag;
  entry on a close above the flag high, stop at the flag low, target = entry
  plus the pole height (capped at 3R).
- CUP-AND-HANDLE (simplified): a 40-bar rounded base 10-25% deep whose right
  side recovers to within 3% of the left rim, then a shallow 5-bar handle;
  entry on a close above the rim, stop at the handle low, target = rim plus
  the cup depth (capped at 3R).

Hallucination guard (compendium: "if it needs squinting, it isn't there"):
patterns are defined by exact numeric predicates only — no fuzzy scoring. If
both patterns fire on the same bar, the flag takes precedence.

Reversal patterns are treated as exit signals rather than fresh shorts: a held
symbol is exited when the close breaks below the floor of the last 10 bars
after price had been more than 5% above it (a reversal footprint).

Regime: liquid large-cap names (NIFTY50_UNIVERSE) where enough participants
watch the same structure for the pattern to be self-reinforcing; needs a tape
with genuine impulses, decays in listless chop.

Primary risk: pattern hallucination — seeing structure in noise. Mitigated by
exact predicates and by predefining invalidation (the stop sits inside the
pattern: flag low / handle low) before entry.

India note: cash-market shorts are intraday-only in India; positional shorts
need futures/puts — omitted here, so this strategy is LONG-only and uses
reversal footprints purely as exits.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta


class ChartPatternsStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw07_chart_patterns",
        name="Classical Chart Patterns (Bull Flag / Cup-and-Handle)",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=80,
        params={
            # bull flag
            "pole_len": 15,             # bars of the impulse pole
            "pole_min_pct": 8.0,        # min pole return, %
            "flag_len": 8,              # bars of the consolidation flag
            "flag_max_pct": 6.0,        # (flag high - flag low)/close ceiling, %
            "flag_drift_max_pct": 1.0,  # flag close change must be <= this, % (flat-to-down)
            # cup-and-handle
            "cup_window": 40,           # bars of the rounded base (excluding today)
            "rim_bars": 10,             # left rim = max close of the first N window bars
            "cup_min_depth_pct": 10.0,  # max drawdown from the rim, %
            "cup_max_depth_pct": 25.0,
            "rim_recover_pct": 3.0,     # right side must close within this % of the rim
            "handle_bars": 5,           # handle length at the right edge of the window
            "handle_max_below_pct": 5.0,  # handle closes drift at most this % below the rim
            # confirmation / risk
            "vol_mult": 1.3,            # breakout volume vs 20-bar mean (when volume exists)
            "vol_avg_bars": 20,
            "rr_cap": 3.0,              # measured-move target capped at this many R
            # reversal-footprint exit
            "exit_floor_bars": 10,      # floor = min low of the last N bars before today
            "exit_above_pct": 5.0,      # ...after price had been > this % above the floor
            "max_new_entries": 2,       # new entries per scan
        },
        capital_required=150_000,
        max_positions=3,
        intraday_squareoff=False,
        description=("LONG-only continuation breakouts from exact-predicate bull flags "
                     "(8%+ pole, tight flat flag) and simplified cup-and-handle bases, "
                     "on 1.3x volume; stops inside the pattern, measured-move targets "
                     "capped at 3R. Reversal footprints exit held names. Risk: pattern "
                     "hallucination — invalidation is predefined at the pattern low."),
    )

    # ------------------------------------------------------------------ patterns
    def _bull_flag(self, df: pd.DataFrame, close: float) -> tuple[float, float] | None:
        """Return (stop, target) if today's close breaks out of a bull flag, else None."""
        p = self.params
        pole_len = int(p["pole_len"])
        flag_len = int(p["flag_len"])
        if len(df) < flag_len + pole_len + 1:
            return None

        pole_end = float(df.close.iloc[-1 - flag_len])          # close[t - flag_len]
        pole_start = float(df.close.iloc[-1 - flag_len - pole_len])
        if pole_start <= 0:
            return None
        pole_return = pole_end / pole_start - 1.0
        if pole_return < p["pole_min_pct"] / 100.0:
            return None

        flag = df.iloc[-(flag_len + 1):-1]                       # flag bars, excluding today
        flag_high = float(flag.high.max())
        flag_low = float(flag.low.min())
        if (flag_high - flag_low) / close > p["flag_max_pct"] / 100.0:
            return None
        first_flag_close = float(flag.close.iloc[0])
        if first_flag_close <= 0:
            return None
        drift = float(flag.close.iloc[-1]) / first_flag_close - 1.0
        if drift > p["flag_drift_max_pct"] / 100.0:              # must be flat-to-down
            return None

        if close <= flag_high:                                   # breakout trigger
            return None
        if flag_low >= close:
            return None

        pole_height = pole_end - pole_start                      # measured move
        target = min(close + pole_height, close + p["rr_cap"] * (close - flag_low))
        return flag_low, target

    def _cup_and_handle(self, df: pd.DataFrame, close: float) -> tuple[float, float] | None:
        """Return (stop, target) if today's close breaks a cup-and-handle rim, else None."""
        p = self.params
        window_len = int(p["cup_window"])
        rim_bars = int(p["rim_bars"])
        handle_bars = int(p["handle_bars"])
        if len(df) < window_len + 1:
            return None

        window = df.iloc[-(window_len + 1):-1]                   # base bars, excluding today
        rim = float(window.close.iloc[:rim_bars].max())          # left rim
        if rim <= 0:
            return None

        trough = float(window.close.min())
        depth = 1.0 - trough / rim                               # max drawdown from the rim
        if not (p["cup_min_depth_pct"] / 100.0 <= depth <= p["cup_max_depth_pct"] / 100.0):
            return None

        pre_handle = window.iloc[:-handle_bars]
        recovery = float(pre_handle.close.iloc[-rim_bars:].max())  # right-side recovery
        if recovery < rim * (1.0 - p["rim_recover_pct"] / 100.0):
            return None

        handle = window.iloc[-handle_bars:]
        if float(handle.close.min()) < rim * (1.0 - p["handle_max_below_pct"] / 100.0):
            return None
        if float(handle.close.max()) > rim:                      # handle stays under the rim
            return None

        if close <= rim:                                         # breakout trigger
            return None
        handle_low = float(handle.low.min())
        if handle_low >= close:
            return None

        cup_depth = rim - trough                                 # measured move
        target = min(rim + cup_depth, close + p["rr_cap"] * (close - handle_low))
        if target <= close:
            return None
        return handle_low, target

    # ------------------------------------------------------------------ signals
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        open_syms = {pos.symbol for pos in ctx.open_positions}
        p = self.params
        new_entries = 0

        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue
            close = float(df.close.iloc[-1])
            if close <= 0:
                continue

            if sym in open_syms:
                # reversal footprint: close breaks the 10-bar floor after price
                # had been more than 5% above it
                floor_bars = int(p["exit_floor_bars"])
                prior = df.iloc[-(floor_bars + 1):-1]
                floor = float(prior.low.min())
                was_extended = float(prior.close.max()) > floor * (1.0 + p["exit_above_pct"] / 100.0)
                if close < floor and was_extended:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason="reversal pattern exit"))
                continue

            if new_entries >= p["max_new_entries"]:
                continue

            # volume expansion confirmation when volume data exists (guard > 0)
            vol_today = float(df.volume.iloc[-1])
            avg_vol = float(df.volume.iloc[-int(p["vol_avg_bars"]):].mean())
            if vol_today > 0 and avg_vol > 0 and vol_today < p["vol_mult"] * avg_vol:
                continue

            # exact predicates only; if both fire on the same bar, take the flag
            flag_hit = self._bull_flag(df, close)
            if flag_hit is not None:
                stop, target = flag_hit
                reason = "bull flag breakout (measured move)"
            else:
                cup_hit = self._cup_and_handle(df, close)
                if cup_hit is None:
                    continue
                stop, target = cup_hit
                reason = "cup-and-handle breakout (measured move)"

            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop, take_profit=target, product_type=ProductType.CNC,
                reason=reason))
            new_entries += 1
        return signals
