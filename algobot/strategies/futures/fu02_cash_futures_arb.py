"""7.2 Cash-Futures Arbitrage (basis capture).

Buy the stock, short its future, pocket the spread — equity risk removed,
carry earned. When the futures premium (basis) annualises above a hurdle,
buy the cash share and short the monthly future one-to-one; unwind at
convergence near expiry.

Edge: the basis is a near-riskless carry, but a single-digit annualised one —
this is a scale game, not a home-run trade. Regime: bullish, leverage-hungry
markets where futures trade rich to cash (long rollers pay up). Primary risk:
execution-leg slippage between the two fills, forced early unwinds when the
basis inverts or margin calls hit, and the temptation to lever the "riskless"
spread — leverage tempts, transaction costs decide whether anything is left.
India note: arbitrage mutual funds productise exactly this trade at scale and
enjoy equity-fund taxation while doing it, which is the retail benchmark to
beat after costs.

LIMITATION (honest): backtests carry no futures quotes, so ctx.extras never
contains "futures_quotes" there and the strategy generates zero trades unless
params["test_basis_annual_pct"] is set to inject an annualised basis directly.
Live/paper runtime must feed ctx.extras["futures_quotes"] as a mapping of
equity symbol -> futures LTP for real entries.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, OptionType, ProductType, Side, SignalType, Timeframe
from algobot.core.models import ExpiryRule, OptionLeg, OptionStructure, Signal, SizeHint, StrikeRule
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta

EXIT_DTE = 2               # unwind when the monthly expiry is this close (convergence)
FALLBACK_MAX_SESSIONS = 20  # expiry calendar unavailable -> time-stop on position age
FALLBACK_DTE = 15          # mid-cycle assumption for annualising when calendar is down


def _short_future_structure(underlying: str) -> OptionStructure:
    """Single-leg short monthly stock future, fu01-style (LegBuilder resolves it)."""
    return OptionStructure(
        name="cash_futures_arb_future",
        underlying=underlying,
        legs=[OptionLeg(side=Side.SELL, option_type=OptionType.FUT,
                        strike_rule=StrikeRule.absolute(0),
                        expiry_rule=ExpiryRule.monthly(0))],
        net_direction="debit",
    )


def _monthly_dte(sym: str, ctx: StrategyContext) -> int | None:
    """Days to the nearest monthly expiry; None when the data layer is unavailable."""
    try:
        from algobot.data.expiries import days_to_expiry
        from algobot.data.instruments import root_of
        return days_to_expiry(root_of(sym), "monthly", on_date=ctx.now.date())
    except Exception:
        return None


class CashFuturesArbStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="fu02_cash_futures_arb",
        name="Cash-Futures Arbitrage (basis capture)",
        category=Category.FUTURES,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["NSE:RELIANCE-EQ"],
        warmup_bars=30,
        params={
            "hurdle_annual_pct": 12.0,  # enter only when basis annualises above this
            "lot_qty": 250,             # cash shares bought == one futures lot
        },
        capital_required=1_800_000,     # full delivery on the cash leg + short-future margin
        max_positions=2,                # one cash leg + one futures leg
        max_trades_per_day=2,           # both legs fire in the same scan
        intraday_squareoff=False,
        is_multi_leg=True,
        description=("Buy the stock, short its monthly future one-to-one when the "
                     "basis annualises above the hurdle; unwind at convergence near "
                     "expiry. Market-neutral carry — costs and leg slippage are the "
                     "real enemy, not direction."),
    )

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        sym, df = next(iter(data.items()))
        if len(df) < self.meta.warmup_bars:
            return []
        p = self.params
        close = float(df.close.iloc[-1])
        dte = _monthly_dte(sym, ctx)

        # ------------------------------------------------------------- exits
        if ctx.open_positions:
            if dte is not None:
                unwind = dte <= EXIT_DTE
                why = f"convergence unwind: {dte}d to monthly expiry"
            else:
                opened = pd.Timestamp(min(pos.opened_at for pos in ctx.open_positions))
                if opened.tzinfo is None and df.index.tz is not None:
                    opened = opened.tz_localize(df.index.tz)
                elif opened.tzinfo is not None and df.index.tz is None:
                    opened = opened.tz_localize(None)
                age_sessions = int((df.index > opened).sum())
                unwind = age_sessions >= FALLBACK_MAX_SESSIONS
                why = f"expiry calendar unavailable: age {age_sessions} sessions"
            if not unwind:
                return []
            return [Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                instrument=pos.symbol, timestamp=ctx.now, reference_price=close,
                product_type=pos.product_type, reason=why)
                for pos in ctx.open_positions]

        # ------------------------------------------------------------ entries
        basis_annual: float | None = None
        fut_ltp = ctx.extras.get("futures_quotes", {}).get(sym)
        if fut_ltp is not None and close > 0:
            ann_days = max(dte if dte is not None else FALLBACK_DTE, 1)
            basis_annual = (float(fut_ltp) - close) / close * (365.0 / ann_days) * 100.0
        else:
            # testability fallback: inject the annualised basis directly
            test_basis = p.get("test_basis_annual_pct")
            if test_basis is not None:
                basis_annual = float(test_basis)

        if basis_annual is None or basis_annual < float(p["hurdle_annual_pct"]):
            return []

        why = (f"basis {basis_annual:.1f}% annualised >= "
               f"hurdle {float(p['hurdle_annual_pct']):.1f}%")
        return [
            Signal(  # cash leg: buy delivery, sized to exactly one futures lot
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_LONG,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=None, size_hint=SizeHint(qty=int(p["lot_qty"])),
                product_type=ProductType.CNC, tags={"leg": "cash"},
                reason=why),
            Signal(  # futures leg: short one monthly lot against the shares
                strategy_id=self.strategy_id, signal_type=SignalType.ENTRY_SHORT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                stop_loss=None, structure=_short_future_structure(sym),
                product_type=ProductType.MARGIN, tags={"leg": "future"},
                reason=why),
        ]
