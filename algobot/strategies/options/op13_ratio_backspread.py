"""6.13 Ratio Spreads and Backspreads — backspread side only.

Sell one nearer option, buy two further-out options of the same type and
expiry, ideally for near-zero net cost: the single richer short funds the two
cheaper longs. Net long convexity — a big move in the chosen direction pays on
the extra long; a quiet tape costs only the small defined debit (or forfeits a
small credit). Works best before potential regime breaks, when volatility has
compressed and a trend is already in place to lean on.

Only the BACKSPREAD side is implemented. Front ratio spreads (buy 1 near, sell
2 far) reintroduce naked-tail risk — the extra uncovered short is a strangle in
disguise — and belong to experienced sellers; they are deliberately omitted.

Edge: convexity bought near-free out of volatility compressions — when the
squeeze resolves into a real regime break, the 2x long wing outruns the 1x
short leg non-linearly.
Regime it needs: pre-regime-break compressions — a Bollinger squeeze firing
with a prevailing trend (50-EMA) supplying the direction to load.
Primary risk: theta bleed if the break never comes — the structure decays
slowly toward its small max loss on a quiet tape; the time stop caps that
bleed, and the move target banks the break when it arrives.
India note: backspreads are margin-light and small-account-friendly — the
short leg is covered twice over, so exchanges margin them like defined-risk
spreads. The naked-extra-short front ratio is a strangle in disguise and is
not implemented here.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema
from algobot.indicators.volatility import bb_squeeze
from algobot.options.structures import ratio_backspread


class RatioBackspreadStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op13_ratio_backspread",
        name="Ratio Backspread (Nifty regime break)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=140,
        params={
            "squeeze_lookback_bars": 5,  # squeeze must have fired within the last N bars
            "trend_ema": 50,             # prevailing trend supplies the direction
            "sell_delta": 0.45,          # short 1x nearer the money
            "buy_delta": 0.25,           # long 2x further out
            "move_target_pct": 2.0,      # underlying move that counts as the break
            "max_hold_days": 10,         # sessions; cap the quiet-tape theta bleed
        },
        capital_required=200_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Sell 1x ~0.45-delta, buy 2x ~0.25-delta same-type monthly "
                     "Nifty options (near-zero-cost long convexity) when a "
                     "Bollinger squeeze fires with the 50-EMA trend supplying "
                     "direction. Exit into a >=2% underlying move or after 10 "
                     "sessions to cap quiet-tape theta bleed."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        close = float(df.close.iloc[-1])
        p = self.params

        # ------------------------------------------------------------- exits
        if ctx.has_open_position:
            # all three legs belong to one structure — one EXIT closes it all,
            # so evaluate once off the first position.
            pos = ctx.open_positions[0]
            entry_ref = pos.underlying_entry if pos.underlying_entry else pos.avg_price
            # (a) move captured — the regime break fired; bank the convexity
            if entry_ref and abs(close - entry_ref) / entry_ref >= p["move_target_pct"] / 100.0:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"move captured: {abs(close - entry_ref) / entry_ref * 100:.2f}% "
                           f">= {p['move_target_pct']:.1f}%")]
            # (b) time stop — the break never came; cap the quiet-tape bleed
            opened_date = pos.opened_at.date()
            sessions_held = int(sum(1 for ts in df.index[-40:] if ts.date() > opened_date))
            if sessions_held > p["max_hold_days"]:
                return [Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    reason=f"time stop: {sessions_held} sessions > {p['max_hold_days']}")]
            return []

        # ------------------------------------------------------------- entry
        # regime break loading: Bollinger squeeze fired within the last N bars
        squeeze = bb_squeeze(df.close, 20, 2.0, lookback=120)
        if not bool(squeeze.iloc[-p["squeeze_lookback_bars"]:].any()):
            return []

        # direction from the prevailing trend (50-EMA)
        trend = ema(df.close, p["trend_ema"])
        if pd.isna(trend.iloc[-1]):
            return []
        trend_now = float(trend.iloc[-1])
        if close > trend_now:
            opt_type, signal_type, side_txt = OptionType.CE, SignalType.ENTRY_LONG, "call"
        elif close < trend_now:
            opt_type, signal_type, side_txt = OptionType.PE, SignalType.ENTRY_SHORT, "put"
        else:
            return []

        # 1x2 backspread on the monthly: sell 1 nearer, buy 2 further out.
        # Defined small cost / near-zero debit is the risk — no underlying stop.
        structure = ratio_backspread(
            sym, opt_type,
            sell_rule=StrikeRule.delta(p["sell_delta"]),
            buy_rule=StrikeRule.delta(p["buy_delta"]),
            expiry_rule=ExpiryRule.monthly())
        return [Signal(
            strategy_id=self.strategy_id, signal_type=signal_type,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=None, structure=structure,
            tags={"trend_ema": trend_now, "direction": side_txt},
            reason=f"regime break loading: bb squeeze in last "
                   f"{p['squeeze_lookback_bars']} bars, close "
                   f"{'>' if close > trend_now else '<'} {p['trend_ema']}-EMA "
                   f"-> {side_txt} backspread")]
