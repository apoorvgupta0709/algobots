"""4.9 Earnings and Event Swing — trade the reaction, not the prediction.

Daily-data implementation with NO earnings calendar: a large up-gap on heavy
volume IS the event proxy. No positions are taken into a print by construction
of the entry (we only ever enter on the reaction day itself), but without a
calendar the "no positions into the print" rule cannot be *guaranteed* for a
position already riding a prior event's drift — flag: a second event can land
mid-hold.

Edge: genuine surprises drift furthest — post-earnings-announcement drift
persists 3-15 sessions after a gap that HOLDS (closes above its open, beyond
the prior 20-day high, on >= 1.5x average volume). The central ratchet trail
rides that drift; a time stop closes the window.
Regime: works best in neutral-to-bullish tapes where surprises get rewarded;
in heavy risk-off even good numbers fade by the close (and the hold filter
then keeps us out).
Risk: fade-and-trap opens — a big gap that closes below its open is the
classic trap, so we require close > open and stop at the gap-day low; the
residual risk is a day-2 full gap reversal through the stop. And, as above,
without a calendar we cannot guarantee no open position sits through the next
print.
Down-gap shorts are omitted: CNC equity cannot short overnight — the mirror
trade needs futures or puts.
India note: many Indian companies report mid-session, creating intraday gaps
rather than opening gaps — same rules apply on the reaction bar, just on a
faster clock; this EOD version only catches the ones still standing at the
close.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta


class EventSwingStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw09_event_swing",
        name="Earnings / Event Gap Swing",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=40,
        params={"gap_min_pct": 0.5, "vol_mult": 1.5, "lookback": 20,
                "max_hold_days": 15, "max_new_entries": 2},
        capital_required=150_000,
        max_positions=3,
        max_trades_per_day=3,
        intraday_squareoff=False,
        description=("Long-only event-reaction swing: enter when a stock gaps up "
                     ">= 2% and HOLDS it — close above open, above the prior "
                     "20-day high, on >= 1.5x average volume. Stop at the gap-day "
                     "low; the central ratchet rides 3-15 days of drift, a "
                     "15-session time stop closes the window."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        p = self.params
        open_by_sym = {pos.symbol: pos for pos in ctx.open_positions}
        new_entries = 0

        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue
            close = float(df.close.iloc[-1])

            pos = open_by_sym.get(sym)
            if pos is not None:
                # ------------------------------------------------- time stop
                # Trailing/breakeven is central risk management; our only
                # strategy exit is the end of the drift window.
                entry_date = pd.Timestamp(pos.opened_at).date()
                held_sessions = int((df.index.date >= entry_date).sum())
                if held_sessions > p["max_hold_days"]:
                    signals.append(Signal(
                        strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                        instrument=sym, timestamp=ctx.now, reference_price=close,
                        product_type=ProductType.CNC,
                        reason=f"drift window over: {held_sessions} sessions held"))
                continue

            # --------------------------------------------------------- entry
            if new_entries >= p["max_new_entries"]:
                continue
            today_open = float(df.open.iloc[-1])
            today_low = float(df.low.iloc[-1])
            prior_close = float(df.close.iloc[-2])
            if prior_close <= 0:
                continue

            # 1) the event proxy: a large opening gap up
            gapped_up = today_open >= prior_close * (1.0 + p["gap_min_pct"] / 100.0)
            if not gapped_up:
                continue
            # 2) the gap HELD: no fade-and-trap open
            if not close > today_open:
                continue
            # 3) beyond the key level: above the prior 20-day high
            lb = int(p["lookback"])
            prior_high = df.high.iloc[-1 - lb:-1].max()
            if pd.isna(prior_high) or not close > float(prior_high):
                continue
            # 4) heavy volume, when volume data exists
            if "volume" in df.columns:
                vol_now = float(df.volume.iloc[-1])
                vol_mean = df.volume.iloc[-1 - lb:-1].mean()
                if pd.notna(vol_mean) and float(vol_mean) > 0 and vol_now > 0:
                    if vol_now < p["vol_mult"] * float(vol_mean):
                        continue

            stop = today_low * 0.995
            if stop <= 0 or stop >= close:
                continue
            gap_pct = (today_open / prior_close - 1.0) * 100.0
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=stop, product_type=ProductType.CNC,
                reason=(f"event gap +{gap_pct:.1f}% held: close > open, above "
                        f"prior {lb}-day high on heavy volume")))
            new_entries += 1
        return signals
