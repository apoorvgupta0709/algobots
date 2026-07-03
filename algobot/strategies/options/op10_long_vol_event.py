"""6.10 Long Straddle / Strangle (event volatility).

Buy the move without picking the direction — and pay the IV toll knowingly.
Buy an ATM straddle 3-10 days before a binary event when IV percentile is
still moderate; exit into the spike or immediately post-event; never hold a
full loser to expiry.

No event calendar is machine-fed here — the volatility-compression proxy IS
the event detector: a Bollinger squeeze at its 120-day minimum with realized
vol cheap (absolute floor OR bottom quartile of its own 120-day range) is the
tape's way of saying the market is coiled ahead of something. The structure is
a long ATM straddle on the NEXT weekly (room for the move to develop).

Edge: when realised movement exceeds what the (compressed) premium implied,
both legs of the debit can't lose at once and one explodes.
Regime it needs: Budgets, elections, RBI surprises — event-adjacent windows
where realised vol can exceed implied.
Primary risk: IV crush. The event passes, the move is average, both legs
deflate simultaneously; the market usually prices events correctly — this
strategy is betting it hasn't. The time stop caps that bleed ("never hold a
full loser"), and the move target sells into the spike before crush.
India note: election-result and Budget-day sessions historically produced the
outsized index moves this strategy needs — and the most brutal IV crushes when
they didn't.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, Side, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volatility import bb_squeeze, realized_vol
from algobot.options.structures import straddle


class LongVolEventStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op10_long_vol_event",
        name="Long Vol Event Straddle (Nifty)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=140,
        params={
            "squeeze_lookback_bars": 5,   # squeeze must have fired within the last N bars
            "rv_max": 0.14,               # absolute cheap-vol floor (annualized fraction)
            "rv_cheap_pctile": 0.25,      # OR: bottom quartile of rv's own 120-day range
            "rv_window": 120,
            "move_exit_pct": 1.5,         # underlying move that counts as "the event fired"
            "max_hold_days": 6,           # sessions; never hold a full loser to expiry
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Buy an ATM straddle on the next Nifty weekly when a "
                     "Bollinger squeeze plus cheap realized vol says the tape is "
                     "coiled (the compression proxy is the event detector). Exit "
                     "into a >=1.5% underlying move or after 6 sessions — never "
                     "hold a full loser to expiry."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        close = float(df.close.iloc[-1])
        p = self.params

        # ------------------------------------------------------------- exits
        if ctx.has_open_position:
            # both straddle legs surface as positions of one structure — one
            # EXIT signal closes the whole structure, so evaluate once.
            pos = ctx.open_positions[0]
            entry_ref = pos.underlying_entry if pos.underlying_entry else pos.avg_price
            # (a) move captured — exit into the spike, before IV deflates
            if entry_ref and abs(close - entry_ref) / entry_ref >= p["move_exit_pct"] / 100.0:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"move captured: {abs(close - entry_ref) / entry_ref * 100:.2f}% "
                           f">= {p['move_exit_pct']:.1f}%")]
            # (b) time stop — the event fizzled; never hold a full loser
            opened_date = pos.opened_at.date()
            sessions_held = int(sum(1 for ts in df.index[-40:] if ts.date() > opened_date))
            if sessions_held > p["max_hold_days"]:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"time stop: {sessions_held} sessions > {p['max_hold_days']}")]
            return []

        # ------------------------------------------------------------- entry
        # coiled: Bollinger squeeze (width at its 120-day minimum) fired recently
        squeeze = bb_squeeze(df.close, 20, 2.0, lookback=120)
        if not bool(squeeze.iloc[-p["squeeze_lookback_bars"]:].any()):
            return []

        # cheap: rv20 under the absolute floor OR in the bottom quartile of its
        # own 120-day range (the OR keeps the strategy alive when the absolute
        # floor never prints in a structurally higher-vol tape)
        rv = realized_vol(df.close, n=20)
        rv_now = rv.iloc[-1]
        if pd.isna(rv_now):
            return []
        rv_now = float(rv_now)
        window = rv.iloc[-int(p["rv_window"]):].dropna()
        cheap_abs = rv_now <= p["rv_max"]
        cheap_rel = False
        if len(window) >= 2:
            lo, hi = float(window.min()), float(window.max())
            cheap_rel = rv_now <= lo + p["rv_cheap_pctile"] * (hi - lo)
        if not (cheap_abs or cheap_rel):
            return []

        # long ATM straddle on the NEXT weekly: room for the move to develop.
        # Defined-risk debit — max loss is the premium paid, so no underlying stop.
        structure = straddle(sym, Side.BUY, expiry_rule=ExpiryRule.weekly(1))
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=None, structure=structure,
            tags={"rv20": rv_now, "cheap_abs": cheap_abs, "cheap_rel": cheap_rel},
            reason=f"coiled tape: bb squeeze in last {p['squeeze_lookback_bars']} bars, "
                   f"rv20 {rv_now * 100:.1f}% cheap "
                   f"({'abs floor' if cheap_abs else 'bottom quartile of 120d range'})")]
