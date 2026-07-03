"""7.3 Futures Calendar Spreads — trade the shape of the curve.

Long one month, short another, direction mostly hedged: when the near-far
futures spread deviates from its fair-carry range, buy the cheap month and
sell the rich one, then exit on normalisation or before the near-month
expiry. The edge concentrates around roll windows, when roll pressure pushes
the spread away from cost-of-carry. Regime needed: an orderly carry market
(no dividend shocks or funding squeezes distorting the curve).

LIMITATION: sizing creep is the real danger on low-vol spreads — the P&L per
lot is tiny, so the temptation is to pile on lots until a curve dislocation
hurts; the engine's caps must stay strict. Backtests trade only with the
``test_spread_pct`` param set (the candle store has no historical futures
curve); live trading needs the runtime to feed ``ctx.extras["futures_curve"]``.
India-specific: the calendar-spread margin benefit vanishes on the near-leg
expiry day (SEBI rule), so the position must be flat before near expiry.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, ProductType, Side, SignalType, Timeframe
from algobot.core.models import ExpiryRule, OptionLeg, OptionStructure, Signal, StrikeRule
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta


def _calendar_structure(underlying: str, near_side: Side, far_side: Side) -> OptionStructure:
    """Two futures legs on the same underlying: near month vs next month."""
    return OptionStructure(
        name="futures_calendar",
        underlying=underlying,
        legs=[
            OptionLeg(side=near_side, option_type=OptionType.FUT,
                      strike_rule=StrikeRule.absolute(0),
                      expiry_rule=ExpiryRule.monthly(0)),
            OptionLeg(side=far_side, option_type=OptionType.FUT,
                      strike_rule=StrikeRule.absolute(0),
                      expiry_rule=ExpiryRule.monthly(1)),
        ],
        net_direction="debit",
    )


class FuturesCalendarSpreadStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="fu03_calendar_spread",
        name="Futures Calendar Spread",
        category=Category.FUTURES,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NIFTY_INDEX"],
        warmup_bars=30,
        params={
            "fair_carry_annual_pct": 6.5,   # fair annualised carry, % of spot
            "dev_min_pct": 0.25,            # min |observed-fair| spread deviation, % of spot
            "norm_exit_pct": 0.10,          # exit when |deviation| falls below this
            "exit_dte": 2,                  # flatten with <=N days to near expiry
            "max_hold_sessions": 15,        # fallback time stop when calendar unavailable
            "test_spread_pct": None,        # backtest/self-test: near-far spread as % of spot
        },
        capital_required=500_000,
        max_positions=1,
        max_trades_per_day=1,
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Near-vs-next-month Nifty futures calendar: when the observed "
                     "spread deviates >0.25% of spot from fair carry (~6.5% p.a.), "
                     "sell the rich month and buy the cheap one; exit on spread "
                     "normalisation or 2 days before near expiry. Direction-hedged; "
                     "primary risk is sizing creep on a low-vol structure."),
    )

    # ------------------------------------------------------------------ helpers
    def _observed_spread_pct(self, root: str, spot: float, ctx: StrategyContext) -> float | None:
        """Near-far spread (far ltp - near ltp) as % of spot, from the runtime
        curve feed, else from the ``test_spread_pct`` param, else None."""
        curve = ctx.extras.get("futures_curve") or {}
        quotes = curve.get(root)
        if quotes:
            near, far = quotes.get("near"), quotes.get("far")
            if near is not None and far is not None and spot > 0:
                return (float(far) - float(near)) / spot * 100.0
        test = self.params.get("test_spread_pct")
        return float(test) if test is not None else None

    @staticmethod
    def _expiry_gap_days(root: str, ctx: StrategyContext) -> int:
        """Calendar days between the near and next monthly expiries (fallback 30)."""
        try:
            from algobot.data.expiries import next_expiry
            on = ctx.now.date()
            gap = (next_expiry(root, "monthly", 1, on_date=on)
                   - next_expiry(root, "monthly", 0, on_date=on)).days
            return gap if gap > 0 else 30
        except Exception:
            return 30

    # ------------------------------------------------------------------ contract
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        p = self.params
        close = float(df.close.iloc[-1])

        try:
            from algobot.data.instruments import root_of
            root = root_of(sym)
        except Exception:
            root = "NIFTY"

        observed = self._observed_spread_pct(root, close, ctx)
        fair = p["fair_carry_annual_pct"] * self._expiry_gap_days(root, ctx) / 365.0
        deviation = (observed - fair) if observed is not None else None

        signals: list[Signal] = []

        # ---------------------------------------------------------- exits
        if ctx.has_open_position:
            # near-month expiry approaching: margin benefit dies on expiry day
            try:
                from algobot.data.expiries import days_to_expiry
                expiry_near = days_to_expiry(root, "monthly", on_date=ctx.now.date()) <= p["exit_dte"]
                exit_why = "near-month expiry approaching"
            except Exception:
                opened = min(pos.opened_at for pos in ctx.open_positions)
                age = sum(1 for t in df.index if t.date() > opened.date())
                expiry_near = age >= p["max_hold_sessions"]
                exit_why = f"time stop: {age} sessions held (expiry calendar unavailable)"

            normalised = deviation is not None and abs(deviation) < p["norm_exit_pct"]
            if normalised:
                exit_why = f"spread normalised: deviation {deviation:+.2f}% of spot"
            if expiry_near or normalised:
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    product_type=ProductType.MARGIN, reason=exit_why))
            return signals

        # ---------------------------------------------------------- entries
        if deviation is None or abs(deviation) < p["dev_min_pct"]:
            return signals

        tags = {"observed_spread_pct": round(observed, 4),
                "fair_spread_pct": round(fair, 4),
                "deviation_pct": round(deviation, 4)}
        if deviation > 0:
            # spread RICH: sell the far (rich) month, buy the near month
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=None,
                structure=_calendar_structure(sym, near_side=Side.BUY, far_side=Side.SELL),
                product_type=ProductType.MARGIN, tags=tags,
                reason=(f"spread rich: observed {observed:.2f}% vs fair {fair:.2f}% "
                        f"(dev {deviation:+.2f}%) — sell far / buy near")))
        else:
            # spread CHEAP: buy the far (cheap) month, sell the near month
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=None,
                structure=_calendar_structure(sym, near_side=Side.SELL, far_side=Side.BUY),
                product_type=ProductType.MARGIN, tags=tags,
                reason=(f"spread cheap: observed {observed:.2f}% vs fair {fair:.2f}% "
                        f"(dev {deviation:+.2f}%) — buy far / sell near")))
        return signals
