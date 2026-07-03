"""3.10 Magic Formula (Greenblatt, India edition).

Rank the market on cheap plus good, buy the intersection, repeat annually.
Rank on earnings yield (EBIT/EV, proxied here by 1/PE) and return on capital
(ROCE); sum the two ranks; buy the top names equal-weight; replace annually.
Financials are excluded — EV/EBIT-style metrics mislead for balance-sheet
businesses.

Edge: a disciplined value-quality hybrid — rules, not stories. The formula
buys statistically cheap, high-return-on-capital businesses without narrative
override, which is exactly what discretionary investors fail to do.

Risk: multi-year underperformance is the price of admission; the formula only
works because it is hard to stick with, and most quit at the worst time.
There are no stops — annual replacement IS the risk management.

India note: a governance/pledging filter is mandatory. The formula cannot
read footnotes, and Indian small/mid-cap value traps often come with pledged
promoter stakes; rows with promoter_pledge >= 10% or mcap below 500 cr are
dropped before ranking.

Data note: the shipped ``config/fundamentals.csv`` is SYNTHETIC PLACEHOLDER
data. This strategy works with whatever rows exist and degrades to no signals
on an empty fundamentals frame; replace the CSV with a real feed before
production use.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.fundamentals import CsvFundamentals, _normalize_symbol


class MagicFormulaStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt10_magic_formula",
        name="Magic Formula (Greenblatt, India)",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=1,
        params={
            "review_month": 4,        # annual rebalance month (April; act otherwise only if flat)
            "top_n": 6,               # names bought, equal weight
            "buffer": 10,             # held names inside this rank are kept at review
            "min_mcap_cr": 500,       # size floor
            "max_pledge_pct": 10,     # governance filter: promoter pledge must be below this
            "financials_exclude": [
                "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK",
                "KOTAKBANK", "BAJFINANCE", "BANKBARODA",
            ],
        },
        capital_required=300_000,
        max_positions=1000,           # accumulation/portfolio strategy: engine cap unused
        max_trades_per_day=20,
        intraday_squareoff=False,
        description=("Annual Magic Formula rebalance: rank non-financials on earnings "
                     "yield (1/PE) plus ROCE, buy the top 6 equal-weight, replace names "
                     "that fall outside the top-10 buffer. Governance filter drops "
                     "pledged promoters. Edge is discipline; risk is multi-year "
                     "underperformance most investors cannot sit through."),
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        # Constructor I/O is allowed; signal-time I/O is not.
        self._fundamentals = CsvFundamentals()

    # ------------------------------------------------------------------ helpers
    def _fundamentals_frame(self, symbols: list[str], ctx: StrategyContext) -> pd.DataFrame:
        """Fundamentals for ``symbols`` — prefer ctx.extras, else the CSV provider."""
        src = ctx.extras.get("fundamentals") or self._fundamentals
        if isinstance(src, pd.DataFrame):
            frame = src.reindex([_normalize_symbol(s) for s in symbols])
            frame.index = pd.Index(symbols, name="symbol")
            return frame
        return src.get(list(symbols))

    def _rank(self, fund: pd.DataFrame) -> pd.Series:
        """Combined Magic Formula rank (lower = better). Empty Series if no rows pass."""
        excluded = {str(s).upper() for s in self.params["financials_exclude"]}
        elig = fund[
            (fund["mcap_cr"] > float(self.params["min_mcap_cr"]))
            & (fund["promoter_pledge"] < float(self.params["max_pledge_pct"]))
            & (fund["pe"] > 0)                       # earnings-yield proxy needs positive PE
            & fund["roce"].notna()
        ]
        keep = [sym for sym in elig.index if _normalize_symbol(sym) not in excluded]
        elig = elig.loc[keep]
        if elig.empty:
            return pd.Series(dtype=float)
        earnings_yield = 1.0 / elig["pe"]
        combined = (earnings_yield.rank(ascending=False)
                    + elig["roce"].rank(ascending=False))
        return combined.sort_values(kind="mergesort")   # stable: deterministic on ties

    # ------------------------------------------------------------------ signals
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        held = {_normalize_symbol(p.symbol): p for p in ctx.open_positions}

        # Annual gate: act in the review month, or bootstrap when nothing is held.
        if ctx.now.month != int(self.params["review_month"]) and held:
            return []

        symbols = list(data.keys())
        if not symbols:
            return []
        fund = self._fundamentals_frame(symbols, ctx)
        if fund.empty:
            return []                                  # degrade gracefully: no data, no trades
        ranked = self._rank(fund)
        if ranked.empty:
            return []

        top_n = int(self.params["top_n"])
        top_buys = list(ranked.index[:top_n])
        buffer_set = {_normalize_symbol(s) for s in ranked.index[:int(self.params["buffer"])]}

        signals: list[Signal] = []

        # Replace: exit held names that fell outside the top buffer.
        for norm_sym, pos in held.items():
            if norm_sym in buffer_set:
                continue
            df = data.get(pos.symbol)
            ref = float(df.close.iloc[-1]) if df is not None and len(df) else float(pos.avg_price)
            if ref <= 0:
                continue
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                instrument=pos.symbol, timestamp=ctx.now, reference_price=ref,
                product_type=ProductType.CNC,
                reason=f"annual review: {norm_sym} outside top-{self.params['buffer']} buffer"))

        # Buy: top-N combined-rank names not already held, equal weight.
        for sym in top_buys:
            if _normalize_symbol(sym) in held:
                continue
            df = data.get(sym)
            if df is None or len(df) < self.meta.warmup_bars:
                continue
            close = float(df.close.iloc[-1])
            if close <= 0:
                continue
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                size_hint=SizeHint(weight=1.0 / top_n),
                product_type=ProductType.CNC,
                reason=(f"magic formula rank {ranked[sym]:.1f} "
                        f"(EY={1.0 / float(fund.loc[sym, 'pe']):.3f}, "
                        f"ROCE={float(fund.loc[sym, 'roce']):.1f}%)")))
        return signals
