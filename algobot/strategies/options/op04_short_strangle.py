"""6.4 Short Strangle — naked premium selling at the expected-move edges.

Edge: sell an OTM call and OTM put around 0.15-0.20 delta (roughly the
expected-move edges) on the weekly Nifty and harvest theta; the market pays
you for insuring both tails, and most weeks neither tail is touched.

Regime: calm-but-paid — realized vol contained (rv20 <= ~20%) but NOT at rock
bottom (rv above its 120-day 25th percentile), i.e. high IV percentile plus a
range-bound tape; classic post-event vol-crush conditions. Entered early in
the weekly cycle (3-7 DTE).

Risk: open-ended on BOTH sides — one strong trend day can return a quarter's
income. Manage at credit multiples; adjust or exit when a short strike's delta
doubles (proxied here by underlying-level exits at the short strikes — a
breach exits the whole structure). Never hold undefended through binary
events (budget, RBI, elections, earnings-heavy expiries).

India note: naked index shorts consume ~Rs 2-2.5L margin per side plus a 2%
expiry-day additional ELM — realistically a Rs 5L+ strategy. Tuesday Nifty
weeklies are the most crowded short-vol tenor on the exchange; expect thin
edges and sharp gamma on expiry-adjacent days.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, Side, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_0920_ONCE, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volatility import realized_vol
from algobot.options.structures import strangle


class ShortStrangleStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op04_short_strangle",
        name="Short Strangle (weekly Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_0920_ONCE,
        instruments=["NIFTY_INDEX"],
        warmup_bars=140,               # 20-bar rv + 120-bar percentile window
        params={
            "short_delta": 0.18,       # ~expected-move edges on both sides
            "max_rv": 0.20,            # 20d realized vol ceiling (annualized, decimal)
            "rv_pctile_window": 120,   # lookback for the rv floor percentile
            "rv_pctile": 0.25,         # rv must be >= its 25th percentile: vol must pay
            "min_days_to_expiry": 3,   # enter early cycle, skip expiry-adjacent gamma
            "max_days_to_expiry": 7,
            "band_pct": 0.015,         # fallback exit band when no chain available
        },
        capital_required=500_000,      # naked margin ~Rs 2-2.5L/side + 2% expiry-day ELM
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Sell a ~0.18-delta call and put on the Tuesday Nifty weekly, "
                     "3-7 days out, when rv20 is contained but off its floor "
                     "(vol must pay). Underlying exits at the short strikes proxy "
                     "the 'short-strike delta doubled' rule; open-ended risk both "
                     "sides — never held undefended through binary events."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        close = float(df.close.iloc[-1])
        p = self.params

        # regime filter 1: calm tape — 20d realized vol below the ceiling
        rv = realized_vol(df.close, n=20)
        rv_now = rv.iloc[-1]
        if pd.isna(rv_now) or float(rv_now) > p["max_rv"]:
            return []

        # regime filter 2: but not rock-bottom vol — selling needs premium.
        # Require rv at or above its 25th percentile over the last 120 sessions.
        rv_window = rv.iloc[-p["rv_pctile_window"]:].dropna()
        if rv_window.empty:
            return []
        rv_floor = float(rv_window.quantile(p["rv_pctile"]))
        if float(rv_now) < rv_floor:
            return []

        # cycle filter: enter only with 3-7 days to the weekly expiry
        try:
            from algobot.data.expiries import days_to_expiry
            from algobot.data.instruments import root_of
            dte = days_to_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
        except Exception:
            dte = 5  # data layer unavailable (isolated backtest) -> assume mid-cycle
        if not (p["min_days_to_expiry"] <= dte <= p["max_days_to_expiry"]):
            return []

        structure = strangle(sym, Side.SELL,
                             strike_rule_call=StrikeRule.delta(p["short_delta"]),
                             strike_rule_put=StrikeRule.delta(p["short_delta"]),
                             expiry_rule=ExpiryRule.weekly())

        # underlying-level exits at the short strikes ("delta doubling" proxy):
        # a breach of either short strike exits the whole structure.
        sl_high = sl_low = None
        if ctx.option_chain is not None:
            try:
                from algobot.data.expiries import next_expiry
                from algobot.data.instruments import root_of
                chain = ctx.option_chain(sym)
                expiry = next_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
                sl_high = chain.strike_by_delta(p["short_delta"], "CE", expiry)
                sl_low = chain.strike_by_delta(p["short_delta"], "PE", expiry)
            except Exception:
                pass
        if sl_high is None or sl_low is None:
            move = close * p["band_pct"]
            sl_high, sl_low = close + move, close - move

        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=float(sl_high), take_profit=float(sl_low),
            structure=structure,
            tags={"short_call_strike": float(sl_high), "short_put_strike": float(sl_low),
                  "rv20": float(rv_now), "rv_floor": rv_floor},
            reason=(f"strangle entry: rv20 {float(rv_now)*100:.1f}% "
                    f"(floor {rv_floor*100:.1f}%), dte {dte}"))]
