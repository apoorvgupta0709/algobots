"""6.15 Delta-Neutral / Gamma Scalping (advanced).

Trade volatility itself: own the straddle, hedge the delta, harvest the
wiggles. Buy a monthly ATM straddle when implied-vol-proxy (realized vol) is
cheap — rv20 in the bottom third of its own 120-day range — but the tape still
whips intraday (mean daily range >= 0.6% of close over the last 5 sessions).
Then re-hedge the position delta whenever the underlying drifts more than
~0.35% from the last hedge anchor, converting realized wiggles into cash while
the long gamma keeps regenerating the position.

Edge: profitable when realised volatility exceeds the implied volatility you
paid — each re-hedge locks in a slice of realized movement that the premium
did not fully charge for.
Regime it needs: cheap vol with a choppy, range-whipping tape; trending or
dead-flat markets both starve the harvest.
Primary risk: theta bleed if realized vol underdelivers — the straddle decays
daily and the scalps must outrun it; hedge friction (spreads, slippage,
brokerage on every rebalance) eats the edge; and operationally this is
full-attention-or-fully-automated, nothing between — a missed hedge turns a
vol trade into an accidental directional bet.
India note: delta-hedging Nifty with futures means 65-lot chunks — hedge
granularity is chunky, so small deltas cannot be flattened precisely and the
capital/margin footprint is large (hence capital_required=500k).

MANDATORY IMPLEMENTATION NOTE: the ``on_position_update`` hook below emits
``SignalType.ADJUST`` signals when the underlying moves past the hedge
trigger. ADJUST signals are journaled but NOT auto-executed as futures hedges
by the runtime — live delta-hedging via one-lot futures is the operator's
step or a future engine feature. The compendium itself says most individuals
are better served by section 6.10 (long vol event straddles); this strategy is
the advanced skeleton with the re-hedge hook wired.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, Side, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Position, Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volatility import realized_vol
from algobot.options.structures import straddle


class GammaScalpStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op15_gamma_scalp",
        name="Delta-Neutral Gamma Scalp (Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=140,
        params={
            "rv_window": 120,             # lookback for the rv rank / low
            "rv_cheap_pctile": 0.33,      # rv20 rank percentile ceiling (bottom third)
            "whip_window": 5,             # sessions for the whippiness gauge
            "whip_min_pct": 0.6,          # mean |high-low|/close floor, in %
            "hedge_trigger_pct": 0.35,    # underlying drift from anchor before re-hedge
            "rv_exit_mult": 1.4,          # exit when rv20 >= mult x 120d rv low
            "move_exit_pct": 2.0,         # |move| from entry that ends the campaign
            "max_hold_days": 12,          # sessions; theta bleed cap
        },
        capital_required=500_000,         # futures hedge granularity: 65-lot chunks
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Buy a monthly ATM Nifty straddle when rv20 sits in the "
                     "bottom third of its 120-day range but the tape still whips "
                     ">=0.6%/day, then emit ADJUST delta re-hedge signals as the "
                     "underlying drifts. Exits: rv expansion captured, a 2% "
                     "underlying move, or a 12-session theta-bleed time stop."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        close = float(df.close.iloc[-1])
        p = self.params

        rv = realized_vol(df.close, n=20)
        rv_now = rv.iloc[-1]
        window = rv.iloc[-int(p["rv_window"]):].dropna()
        if pd.isna(rv_now) or len(window) < 2:
            return []
        rv_now = float(rv_now)
        rv_low = float(window.min())

        # ------------------------------------------------------------- exits
        if ctx.has_open_position:
            # all straddle legs belong to one structure — one EXIT closes it.
            pos = ctx.open_positions[0]
            entry_ref = pos.underlying_entry if pos.underlying_entry else pos.avg_price
            # (a) vol expansion captured: implied was paid cheap, realized showed up
            if rv_low > 0 and rv_now >= p["rv_exit_mult"] * rv_low:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"vol expansion captured: rv20 {rv_now * 100:.1f}% >= "
                           f"{p['rv_exit_mult']:.1f}x 120d low {rv_low * 100:.1f}%")]
            # (b) the underlying ran away — this is a wiggle harvest, not a trend ride
            if entry_ref and abs(close - entry_ref) / entry_ref >= p["move_exit_pct"] / 100.0:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"move exit: {abs(close - entry_ref) / entry_ref * 100:.2f}% "
                           f">= {p['move_exit_pct']:.1f}% from entry")]
            # (c) time stop — theta bleed cap when realized never shows up
            opened_date = pos.opened_at.date()
            sessions_held = int(sum(1 for ts in df.index[-40:] if ts.date() > opened_date))
            if sessions_held > p["max_hold_days"]:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"time stop: {sessions_held} sessions > {p['max_hold_days']} "
                           f"(theta bleed cap)")]
            return []

        # ------------------------------------------------------------- entry
        # cheap: rv20 rank percentile within its own 120-day history <= bottom third
        rank_pctile = float((window <= rv_now).mean())
        if rank_pctile > p["rv_cheap_pctile"]:
            return []

        # whippy: mean daily |high-low|/close over the last N sessions — the
        # wiggles must exist for the hedges to harvest anything
        tail = df.iloc[-int(p["whip_window"]):]
        whip = float(((tail.high - tail.low).abs() / tail.close).mean())
        if pd.isna(whip) or whip < p["whip_min_pct"] / 100.0:
            return []

        # long ATM straddle on the current MONTHLY: room for the harvest.
        # Defined-risk debit — max loss is the premium paid, so no underlying stop.
        structure = straddle(sym, Side.BUY, expiry_rule=ExpiryRule.monthly(0))
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=None, structure=structure,
            tags={"rv20": rv_now, "rv_rank_pctile": rank_pctile, "whip": whip},
            reason=f"cheap-but-whippy: rv20 {rv_now * 100:.1f}% at rank pctile "
                   f"{rank_pctile:.2f} <= {p['rv_cheap_pctile']:.2f}, whip "
                   f"{whip * 100:.2f}% >= {p['whip_min_pct']:.2f}%")]

    # ------------------------------------------------------------- re-hedging
    def on_position_update(self, position: Position, ltp_map: dict[str, float],
                           ctx: StrategyContext) -> list[Signal]:
        """Delta re-hedge trigger: when the underlying drifts more than
        ``hedge_trigger_pct`` from the last hedge anchor (``trail_anchor``,
        falling back to ``underlying_entry``), emit an ADJUST signal.

        NOTE: ADJUST signals are journaled but not auto-executed as futures
        hedges by the runtime — executing the one-lot futures rebalance is the
        operator's step (or a future engine feature). See module docstring.
        """
        anchor = position.trail_anchor or position.underlying_entry
        if not anchor:
            return []
        sym = position.underlying or position.symbol
        ltp = ltp_map.get(sym)
        if ltp is None:
            ltp = anchor  # fallback to last known anchor: no fresh price, no move
        ltp = float(ltp)
        move = abs(ltp - anchor) / anchor
        if move < self.params["hedge_trigger_pct"] / 100.0:
            return []
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ADJUST,
            instrument=sym, timestamp=ctx.now, reference_price=ltp,
            reason="delta re-hedge", tags={"hedge": "rebalance"})]
