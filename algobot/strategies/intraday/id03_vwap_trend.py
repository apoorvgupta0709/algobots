"""5.3 VWAP Trend Rides — buy the first kisses of VWAP on trend days.

Edge: on genuine trend days session VWAP flips from mean-reversion magnet to
dynamic support (resistance on down days); institutional execution algos
defend it, so the first one or two pullbacks into VWAP that print a rejection
candle offer tight-stop continuation entries.

Regime: needs a one-sided trend day — a directional first bar and every close
of the first hour (09:15-10:15) held on one side of session VWAP. Useless on
balanced/range days, where VWAP gets crossed repeatedly.

Risk: a trend day that fails mid-session slices straight through VWAP; the
third-plus touch usually breaks, so entries stop after two touches and the
stop sits only a few ticks beyond VWAP. No fresh risk through the 11:30-13:15
lunch chop or after 14:30. Executed via ATM debit verticals (bull call spreads
long, bear put spreads on below-VWAP trend days) so premium at risk is capped.

India note: Bank Nifty trend days are the cleanest VWAP riders, but its speed
demands faster management (quicker breakeven, smaller size) than Nifty.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from algobot.core.enums import Category, OptionType, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal, StrikeRule
from algobot.core.strategy import SCAN_EVERY_5MIN, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volume import vwap
from algobot.options.structures import vertical_spread


class VWAPTrendRideStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="id03_vwap_trend",
        name="VWAP Trend Rides",
        category=Category.INTRADAY,
        timeframe=Timeframe.MIN5,
        scan_schedule=SCAN_EVERY_5MIN,
        instruments=["NIFTY_INDEX"],
        warmup_bars=13,               # full first hour of 5-min bars + first bar
        params={
            "first_hour_end": "10:15",   # trend-day qualification window end
            "lunch_start": "11:30",      # no entries in the lunch chop
            "lunch_end": "13:15",
            "last_entry": "14:30",
            "touch_band_pct": 0.1,       # low within 0.1% above VWAP counts as a touch
            "stop_buffer_pct": 0.1,      # stop a few ticks beyond VWAP
            "max_touches": 2,            # third-plus touch usually breaks
            "rr_target": 1.5,
            "spread_width_steps": 4,     # debit vertical width in strike steps
        },
        capital_required=100_000,
        max_positions=1,
        max_trades_per_day=2,
        intraday_squareoff=True,
        is_multi_leg=True,
        description=("On trend days (one-sided open, first hour held on one side "
                     "of session VWAP) buy the first two pullbacks that kiss VWAP "
                     "and reject; stop a few ticks beyond VWAP, ~1.5R target. "
                     "Third touch usually breaks - stand aside after two."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        first_hour_end = dt.time.fromisoformat(self.params["first_hour_end"])
        lunch_start = dt.time.fromisoformat(self.params["lunch_start"])
        lunch_end = dt.time.fromisoformat(self.params["lunch_end"])
        last_entry = dt.time.fromisoformat(self.params["last_entry"])
        band = self.params["touch_band_pct"] / 100.0
        buffer = self.params["stop_buffer_pct"] / 100.0
        max_touches = int(self.params["max_touches"])
        rr = self.params["rr_target"]
        width = int(self.params["spread_width_steps"])

        signals: list[Signal] = []
        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue
            today = ctx.now.date()
            day = df[df.index.date == today]
            if day.empty:
                continue

            # entry window: after the first hour, outside lunch, before last entry
            bar_time = day.index[-1].time()
            if not (first_hour_end <= bar_time <= last_entry):
                continue
            if lunch_start <= bar_time < lunch_end:
                continue

            v = vwap(day)

            # ---- trend-day classification on the first hour ----
            first_hour = day[day.index.time < first_hour_end]
            if len(first_hour) < 10:      # need (nearly) the full first hour
                continue
            fh_vwap = v.loc[first_hour.index]
            first_bar = day.iloc[0]
            up_open = float(first_bar.close) > float(first_bar.open)
            dn_open = float(first_bar.close) < float(first_bar.open)
            up_trend = up_open and bool((first_hour.close > fh_vwap).all())
            dn_trend = dn_open and bool((first_hour.close < fh_vwap).all())
            if not (up_trend or dn_trend):
                continue

            # ---- pullback touches after the first hour ----
            post = day[day.index.time >= first_hour_end]
            pv = v.loc[post.index]
            if up_trend:
                touching = post.low <= pv * (1.0 + band)
            else:
                touching = post.high >= pv * (1.0 - band)
            if not bool(touching.iloc[-1]):
                continue

            # group consecutive touch bars into one pullback event; count events
            starts = touching & ~touching.shift(1, fill_value=False)
            event_num = int(starts.cumsum().iloc[-1])
            if event_num > max_touches:
                continue                  # third-plus touch usually breaks

            # ---- rejection candle on the just-closed bar ----
            close = float(post.close.iloc[-1])
            bar_open = float(post.open.iloc[-1])
            vwap_now = float(pv.iloc[-1])
            if up_trend:
                rejected = close > vwap_now and close > bar_open
            else:
                rejected = close < vwap_now and close < bar_open
            if not rejected:
                continue

            # only the first rejecting bar of this pullback event fires
            same_event = touching & (starts.cumsum() == event_num)
            prior = post[same_event].iloc[:-1]
            if not prior.empty:
                pv_prior = pv.loc[prior.index]
                if up_trend:
                    already = ((prior.close > pv_prior) & (prior.close > prior.open)).any()
                else:
                    already = ((prior.close < pv_prior) & (prior.close < prior.open)).any()
                if already:
                    continue

            if up_trend:
                stop = vwap_now * (1.0 - buffer)
                if stop >= close:
                    continue
                risk = close - stop
                structure = vertical_spread(
                    sym, OptionType.CE, "debit",
                    buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(width),
                    expiry_rule=ExpiryRule.weekly())
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=close + rr * risk,
                    structure=structure,
                    reason=(f"VWAP trend ride long: touch #{event_num} rejected off "
                            f"VWAP {vwap_now:.1f}, close {close:.1f}")))
            else:
                stop = vwap_now * (1.0 + buffer)
                if stop <= close:
                    continue
                risk = stop - close
                structure = vertical_spread(
                    sym, OptionType.PE, "debit",
                    buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(-width),
                    expiry_rule=ExpiryRule.weekly())
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    stop_loss=stop, take_profit=close - rr * risk,
                    structure=structure,
                    reason=(f"VWAP trend ride short: touch #{event_num} rejected off "
                            f"VWAP {vwap_now:.1f}, close {close:.1f}")))
        return signals
