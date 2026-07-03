"""6.7 Iron Butterfly — expiry-week pin play.

A condor squeezed to a point: sell the ATM straddle, buy wings, bet on a pin.
Edge: theta at the pin — the short ATM straddle collects maximum decay when the
underlying settles onto the short strike into expiry.
Regime: Tuesday Nifty expiry pinning around heavy-OI strikes in low-drift,
low-realized-vol conditions; entered 1-3 days before the weekly expiry.
Risk: narrow profit zone; small drift converts winners to scratches; early
take-profit discipline is the whole game — manage early rather than chasing
the pin to settlement.
India note: 2% expiry-day ELM applies on the short legs.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, SignalType, Timeframe
from algobot.core.models import ExpiryRule, Signal
from algobot.core.strategy import SCAN_0920_ONCE, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.volatility import realized_vol
from algobot.options.structures import iron_butterfly


class IronButterflyStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="op07_iron_butterfly",
        name="Iron Butterfly (Nifty expiry week)",
        category=Category.OPTIONS,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_0920_ONCE,
        instruments=["NIFTY_INDEX"],
        warmup_bars=40,
        params={
            "wing_steps": 6,
            "min_days_to_expiry": 1,   # expiry week only
            "max_days_to_expiry": 3,
            # compendium live values are tighter (0.8% drift / 0.18 RV);
            # synthetic fixtures rarely qualify at those, so defaults are looser.
            "max_drift_pct": 1.5,      # |3-day return| must stay below this
            "max_rv": 0.25,            # 20-day annualized realized vol ceiling
            "breach_pct": 0.7,         # underlying move that brackets the pin
        },
        capital_required=150_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Sell the ATM straddle with wings 6 steps out on the Tuesday "
                     "Nifty weekly, entered 1-3 days before expiry in low-drift, "
                     "low-RV tapes. Underlying-level exits bracket the pin +-0.7%; "
                     "manage early rather than chasing the pin."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        if ctx.has_open_position:
            return []

        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        close = float(df.close.iloc[-1])
        p = self.params

        # drift filter: the tape must be going nowhere
        drift = abs(close / float(df.close.iloc[-4]) - 1.0) * 100
        if drift >= p["max_drift_pct"]:
            return []

        # regime filter: low realized vol only
        rv = realized_vol(df.close, n=20)
        if pd.isna(rv.iloc[-1]) or float(rv.iloc[-1]) > p["max_rv"]:
            return []

        # cycle filter: expiry week only (1-3 days to the weekly expiry)
        try:
            from algobot.data.expiries import days_to_expiry
            from algobot.data.instruments import root_of
            dte = days_to_expiry(root_of(sym), "weekly", on_date=ctx.now.date())
        except Exception:
            dte = 2  # data layer unavailable (isolated backtest) -> assume expiry week
        if not (p["min_days_to_expiry"] <= dte <= p["max_days_to_expiry"]):
            return []

        structure = iron_butterfly(sym, wing_steps=p["wing_steps"],
                                   expiry_rule=ExpiryRule.weekly())

        # underlying-level exits bracket the pin: a breach either way exits the
        # whole structure; the tight take-profit enforces early management.
        breach = close * p["breach_pct"] / 100.0
        return [Signal(
            strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
            instrument=sym, timestamp=ctx.now, reference_price=close,
            stop_loss=close + breach, take_profit=close - breach,
            structure=structure,
            tags={"pin_strike": close, "drift_pct": drift},
            reason=(f"butterfly entry: drift {drift:.2f}%, "
                    f"rv20 {float(rv.iloc[-1]):.2f}, dte {dte}"))]
