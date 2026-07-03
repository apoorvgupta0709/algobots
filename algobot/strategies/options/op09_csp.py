"""6.9 Cash-Secured Put — get paid to place a limit order.

Edge: on quality F&O names you would gladly own, sell monthly puts 5-8% OTM
into sentiment-driven dips, with cash reserved for assignment. Keep the
premium if unexercised; take delivery if exercised and switch to covered
calls (the wheel, 6.9 into 6.8).

Regime: elevated IV in fundamentally sound names after sentiment drops — the
long-term trend intact (close above the 200-day SMA) but the name pulled back
to support (near its 60-day low or below the 20-day SMA), which is exactly
when put premiums are rich.

Risk: assignment happens precisely when the stock breaks down — the strategy
only works if the ownership thesis was real. The exit is therefore an
underlying-level breach BELOW the short strike (strike less breach_pct): if
even the thesis level fails, get out rather than average into a falling
knife. Engine note: per the platform convention for ENTRY_SHORT structures
(see op06 and the authoring guide), a downside breach level is carried in
the ``take_profit`` slot — the monitor exits the whole structure when the
underlying trades below it; there is no separate profit target because
expiry settlement keeps the premium.

India note: the wheel is workable on liquid stock F&O, but contract values
run Rs 15L+ — cash-securing each name is a multi-lakh commitment
(capital_required reflects the reserved assignment cash).
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, Side, SignalType, Timeframe
from algobot.core.models import ExpiryRule, OptionLeg, OptionStructure, Signal, StrikeRule
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import sma


class CashSecuredPutStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op09_csp",
        name="Cash-Secured Put (monthly, quality F&O names)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NSE:RELIANCE-EQ"],
        warmup_bars=210,
        params={
            "otm_pct": 6.0,        # short put ~6% OTM (5-8% band per compendium)
            "breach_pct": 3.0,     # exit if underlying breaks strike by this much
            "trend_sma": 200,      # long-term quality/trend filter
            "dip_sma": 20,         # sentiment-dip proxy: close at/below 20-day SMA
            "low_lookback": 60,    # support shelf lookback
            "near_low_pct": 10.0,  # ... or close within 10% of the 60-day low
        },
        capital_required=600_000,  # cash reserved for assignment: multi-lakh per name
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Sell a monthly put ~6% OTM on a quality F&O name when the "
                     "long-term trend is intact (close > 200-SMA) but the name has "
                     "dipped (near its 60-day low or below the 20-SMA). Cash is "
                     "reserved for assignment; a breach 3% below the strike exits "
                     "the structure, expiry settlement keeps the premium otherwise."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        p = self.params
        signals: list[Signal] = []
        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue
            close = float(df.close.iloc[-1])

            # long-term quality intact: constructive above the 200-day SMA
            sma200 = sma(df.close, int(p["trend_sma"]))
            if pd.isna(sma200.iloc[-1]) or close <= float(sma200.iloc[-1]):
                continue

            # ... but in a dipped spot: near the 60-day low OR a sentiment dip
            # to/below the 20-day SMA (support-shelf simplification)
            low60 = float(df.low.rolling(int(p["low_lookback"])).min().iloc[-1])
            sma20 = sma(df.close, int(p["dip_sma"]))
            near_low = close <= low60 * (1.0 + p["near_low_pct"] / 100.0)
            dipped = (not pd.isna(sma20.iloc[-1])) and close <= float(sma20.iloc[-1])
            if not (near_low or dipped):
                continue

            # single short monthly put, ~otm_pct% OTM — premium collected as credit
            structure = OptionStructure(
                name="cash_secured_put",
                underlying=sym,
                legs=[OptionLeg(side=Side.SELL, option_type=OptionType.PE,
                                strike_rule=StrikeRule.pct_otm(p["otm_pct"]),
                                expiry_rule=ExpiryRule.monthly())],
                net_direction="credit",
            )

            # resolve the actual put strike (spot less otm_pct, rounded per the
            # chain's strike step); fallback to the unrounded level when the
            # data layer is unavailable (isolated backtest/test contexts)
            strike = None
            if ctx.leg_builder is not None:
                try:
                    resolved = ctx.leg_builder.resolve(structure, close, ctx.now)
                    strike = resolved.legs[0].resolved_strike
                except Exception:
                    strike = None
            if strike is None:
                strike = close * (1.0 - p["otm_pct"] / 100.0)

            # thesis-fail exit: you will be assigned precisely when the stock has
            # broken down — exit if the underlying breaches breach_pct BELOW the
            # strike. On ENTRY_SHORT structures the engine's downside breach level
            # is the take_profit slot (op06 convention); expiry settlement keeps
            # the premium otherwise — there is no separate profit target.
            breach_level = float(strike) * (1.0 - p["breach_pct"] / 100.0)

            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=None, take_profit=breach_level,
                structure=structure,
                tags={"put_strike": float(strike), "breach_level": breach_level,
                      "sma200": float(sma200.iloc[-1]), "low60": low60,
                      "near_low": bool(near_low), "dipped": bool(dipped)},
                reason=(f"CSP entry: close {close:.1f} > 200SMA "
                        f"{float(sma200.iloc[-1]):.1f}, "
                        f"{'near 60d low' if near_low else 'below 20SMA'}; "
                        f"short {p['otm_pct']:.0f}% OTM put @ {float(strike):.1f}, "
                        f"breach exit {breach_level:.1f}")))
        return signals
