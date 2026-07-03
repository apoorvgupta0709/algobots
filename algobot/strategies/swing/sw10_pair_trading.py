"""4.10 Pair Trading / Statistical Arbitrage — bet on a relationship, not a direction.

Long the laggard, short the leader when a stable spread stretches. For each
candidate pair (A, B) a hedge ratio beta = cov(logA, logB) / var(logB) is fit
over the trailing lookback window; the spread logA - beta*logB is z-scored
against its rolling mean/std. |z| beyond z_entry means the relationship is
stretched: z > +2 -> A is rich vs B, so SHORT A / LONG B; z < -2 -> A is cheap,
so LONG A / SHORT B. The second leg rides on the signal via
``pair_leg=PairLeg(symbol=B, hedge_ratio=beta)`` (short the rich name, long the
cheap one, hedge-ratio sized).

Edge: mean reversion of a cointegrated spread is (near) market-neutral — it
pays in the range-bound, low-trend regimes where directional systems starve,
and it is indifferent to index direction.
Regime: sideways/rotational markets with historically stable relationships
between the paired names; it needs the spread's variance to be stationary.
Risk: cointegration BREAKS — a re-rating, merger, or divergent news flow makes
a stretched spread stretch forever and never mean-revert; that tail is capped
by the |z| >= z_stop exit ("cointegration break") and a 25-session time stop.
Costs bite double: every round trip pays brokerage/impact on TWO legs, so the
per-trade expectancy must clear twice the friction.

Stop approximation: the ``stop_loss`` on each entry is +/- 2 * ATR(14) from
entry on instrument A only — the risk engine needs a price-space stop to size
the trade. The TRUE risk boundary is the spread itself: |z| >= z_stop (3.0),
enforced by the EXIT logic in ``generate_signals``.

LIMITATION (backtests): the backtester trades only the PRIMARY leg of pair
signals — it logs a warning for ``pair_leg`` and opens no hedge — so backtest
results are a HALF-HEDGED approximation (a directional trade on A timed by the
spread). Live/paper execution routes BOTH legs through the order manager,
sizing the hedge leg as ``round(qty * hedge_ratio)``.

India note: shorting equities beyond intraday is not possible in the cash
market, so the short leg lives in single-stock FUTURES (ProductType.MARGIN /
NRML) — BOTH names of a pair must be on the F&O list, and one lot each side
means several lakh of margin as the practical minimum ticket.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import PairLeg, Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.core.universes import PAIR_CANDIDATES
from algobot.indicators.volatility import atr


class PairTradingStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw10_pair_trading",
        name="Pair Trading / Statistical Arbitrage",
        category=Category.SWING,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_EOD,
        instruments=["PAIR_UNIVERSE"],
        warmup_bars=140,
        params={"lookback": 90, "z_entry": 2.0, "z_exit": 0.3, "z_stop": 3.0,
                "max_hold_sessions": 25, "atr_period": 14, "atr_mult": 2.0},
        capital_required=400_000,
        max_positions=2,               # two legs of one pair
        max_trades_per_day=2,
        intraday_squareoff=False,
        is_pair=True,
        description=("Market-neutral spread mean reversion on cointegration "
                     "candidates: short the rich leg / long the cheap leg when "
                     "the hedge-ratio spread's z-score exceeds 2, exit near 0.3, "
                     "bail at |z|>3 (cointegration break) or 25 sessions. Pays "
                     "in range markets where directional systems starve; the "
                     "killer risk is a spread that breaks and never reverts."),
    )

    # ------------------------------------------------------------------ helpers
    def _zscore_and_beta(self, dfa: pd.DataFrame, dfb: pd.DataFrame,
                         lookback: int) -> tuple[float, float] | None:
        """(z, beta) of spread = logA - beta*logB over the last `lookback`
        aligned bars, or None when the pair is not computable."""
        common = dfa.index.intersection(dfb.index)
        if len(common) < lookback:
            return None
        la = np.log(dfa.close.reindex(common).astype(float)).tail(lookback)
        lb = np.log(dfb.close.reindex(common).astype(float)).tail(lookback)
        var_b = float(lb.var())
        if not np.isfinite(var_b) or var_b <= 0:
            return None
        beta = float(la.cov(lb)) / var_b
        if not np.isfinite(beta) or beta <= 0:
            return None                        # no sensible hedge relationship
        spread = la - beta * lb
        mean = float(spread.mean())
        std = float(spread.std())
        if not np.isfinite(std) or std <= 0:
            return None
        z = (float(spread.iloc[-1]) - mean) / std
        if not np.isfinite(z):
            return None
        return z, beta

    def _exit_pair(self, sym_a: str, sym_b: str, dfa: pd.DataFrame,
                   dfb: pd.DataFrame, ctx: StrategyContext,
                   reason: str) -> list[Signal]:
        """EXIT both symbols (harmless no-op for a leg with no open position)."""
        out = []
        for sym, df in ((sym_a, dfa), (sym_b, dfb)):
            out.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                instrument=sym, timestamp=ctx.now,
                reference_price=float(df.close.iloc[-1]),
                product_type=ProductType.MARGIN, reason=reason))
        return out

    # ------------------------------------------------------------------ signals
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        signals: list[Signal] = []
        p = self.params
        open_by_sym = {pos.symbol: pos for pos in ctx.open_positions}

        for sym_a, sym_b in PAIR_CANDIDATES:
            dfa, dfb = data.get(sym_a), data.get(sym_b)
            if dfa is None or dfb is None:
                continue
            if len(dfa) < p["lookback"] or len(dfb) < p["lookback"]:
                continue
            zb = self._zscore_and_beta(dfa, dfb, p["lookback"])
            if zb is None:
                continue
            z, beta = zb

            legs_open = [open_by_sym[s] for s in (sym_a, sym_b) if s in open_by_sym]
            if legs_open:
                # ------------------------------------------------------ exits
                if abs(z) <= p["z_exit"]:
                    signals.extend(self._exit_pair(
                        sym_a, sym_b, dfa, dfb, ctx,
                        reason=f"spread converged: |z| {abs(z):.2f} <= {p['z_exit']}"))
                    continue
                if abs(z) >= p["z_stop"]:
                    signals.extend(self._exit_pair(
                        sym_a, sym_b, dfa, dfb, ctx,
                        reason=(f"cointegration break: |z| {abs(z):.2f} >= "
                                f"{p['z_stop']}")))
                    continue
                # time stop: sessions since the earliest leg entry
                entry_date = min(pd.Timestamp(pos.opened_at).date()
                                 for pos in legs_open)
                held_sessions = int((dfa.index.date >= entry_date).sum())
                if held_sessions > p["max_hold_sessions"]:
                    signals.extend(self._exit_pair(
                        sym_a, sym_b, dfa, dfb, ctx,
                        reason=f"time stop: {held_sessions} sessions held"))
                continue

            # ---------------------------------------------------------- entries
            if not (p["z_entry"] <= abs(z) < p["z_stop"]):
                continue
            close_a = float(dfa.close.iloc[-1])
            atr_a = atr(dfa, n=p["atr_period"]).iloc[-1]
            if pd.isna(atr_a) or float(atr_a) <= 0:
                continue
            stop_dist = p["atr_mult"] * float(atr_a)
            if z > 0:
                # A rich vs B: short A, long B (hedge leg carried on the signal)
                side, stop = SignalType.ENTRY_SHORT, close_a + stop_dist
            else:
                # A cheap vs B: long A, short B
                side, stop = SignalType.ENTRY_LONG, close_a - stop_dist
                if stop <= 0:
                    continue
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=side,
                instrument=sym_a, timestamp=ctx.now, reference_price=close_a,
                stop_loss=stop, pair_leg=PairLeg(symbol=sym_b, hedge_ratio=beta),
                product_type=ProductType.MARGIN,
                reason=(f"spread stretched: z {z:+.2f}, beta {beta:.2f} vs "
                        f"{sym_b} (ATR stop approximates the |z|>"
                        f"{p['z_stop']:.0f} spread stop)")))
        return signals
