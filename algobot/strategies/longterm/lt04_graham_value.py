"""3.4 Value Investing — Graham-style screens.

Buy a rupee of assets or earnings for sixty paise, diversified enough to
survive being early. Screen for low P/E, price-to-book below 1.5,
debt-to-equity below 0.5, positive cash flow proxies and a dividend record;
hold roughly equal weight and review the book annually.

Edge: paying materially less than conservative asset/earnings value gives a
margin of safety that compensates for being wrong or early on any one name.
Regime: works best coming OUT of post-crash markets and in unloved sectors,
where indiscriminate selling leaves sound balance sheets at distressed
multiples; it lags in momentum-led, expensive markets.
Primary risk: value traps — statistically cheap businesses that deserve to be
cheap (secular decline, capital misallocation) — and governance discounts
that are often deserved rather than mispriced.
India note: governance screens come FIRST. Screen out promoter pledging and
serial equity diluters BEFORE looking at price ratios; in India a low
multiple frequently prices exactly those problems, not a bargain.

Placeholder-CSV note: `config/fundamentals.csv` is synthetic placeholder
data. With the default `max_pe=20` (the compendium's 12-14 is deliberately
relaxed — see params comment) only SBIN passes the full screen on that file.
The strategy degrades to no signals on an empty fundamentals frame; replace
the CSV with a real feed before production use.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.fundamentals import CsvFundamentals, screen


class GrahamValueScreen(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt04_graham_value",
        name="Graham Value Screen",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=30,
        params={
            # Compendium §3.4 prescribes P/E below 12-14. The default here is
            # 20 because the shipped placeholder CSV is large-cap heavy and a
            # 14 cutoff combined with pb<=1.5 passes nothing; at 20 roughly
            # one name (SBIN) survives. Tighten to 14 on a real feed.
            "max_pe": 20.0,
            "max_pb": 1.5,          # price-to-book at or below 1.5
            "max_de": 0.5,          # debt-to-equity below 0.5
            "min_div_yield": 0.0,   # strict '>': a dividend record required
            "max_pledge": 10.0,     # GOVERNANCE stage: promoter pledge < 10%
            "max_names": 10,        # equal weight across up to this many names
            "review_months": [4, 10],  # annual/semi-annual review gate
        },
        capital_required=300_000,
        max_positions=1000,          # portfolio screen: engine never caps names
        max_trades_per_day=25,
        intraday_squareoff=False,
        description=("Graham value screen on the NIFTY50 universe: governance first "
                     "(promoter pledge <10%), then P/E, P/B<=1.5, D/E<0.5 and a "
                     "dividend record. Equal-weight CNC accumulation, reviewed in "
                     "April/October; exits names that leave the screen. Risk is the "
                     "value trap — the screen plus diversification is the stop."),
    )

    def __init__(self, params: Optional[dict] = None):
        super().__init__(params)
        # Constructor I/O is allowed; generate_signals stays pure. Prefer
        # ctx.extras["fundamentals"] at signal time when the runtime provides it.
        self._fundamentals = CsvFundamentals()

    # ------------------------------------------------------------------ screen
    def _passing(self, fund: pd.DataFrame) -> pd.DataFrame:
        """Two-stage screen: governance first, then value ratios."""
        governed = screen(fund, {
            "promoter_pledge": ("<", float(self.params["max_pledge"])),
        })
        return screen(governed, {
            "pe": ("<", float(self.params["max_pe"])),
            "pb": ("<=", float(self.params["max_pb"])),
            "de_ratio": ("<", float(self.params["max_de"])),
            "dividend_yield": (">", float(self.params["min_div_yield"])),
        })

    # ------------------------------------------------------------------ signals
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        provider = ctx.extras.get("fundamentals") or self._fundamentals
        symbols = sorted(data.keys())
        if not symbols:
            return []
        fund = provider.get(symbols)
        if fund.empty or fund.dropna(how="all").empty:
            return []  # degrade gracefully: no fundamentals, no opinions

        # Review gate: act only in a review month, or when holding nothing
        # (initial build / fully-exited book keeps trying monthly).
        held = {p.symbol for p in ctx.open_positions}
        if held and ctx.now.month not in list(self.params["review_months"]):
            return []

        passed = self._passing(fund)
        passing = set(passed.index)

        # Target book: screened names present in the data universe with enough
        # history, cheapest P/E first, equal weight up to max_names.
        ranked = passed.sort_values("pe", kind="mergesort")
        target = [s for s in ranked.index
                  if s in data and len(data[s]) >= self.meta.warmup_bars]
        target = target[: int(self.params["max_names"])]

        signals: list[Signal] = []
        if target:
            weight = 1.0 / len(target)
            for sym in target:
                if sym in held:
                    continue  # already owned and still passing: hold
                close = float(data[sym].close.iloc[-1])
                if close <= 0:
                    continue
                signals.append(Signal(
                    strategy_id=self.strategy_id,
                    signal_type=SignalType.REBALANCE,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    size_hint=SizeHint(weight=weight),
                    product_type=ProductType.CNC,
                    reason=(f"Graham screen entry: pe={passed.at[sym, 'pe']:.1f}, "
                            f"pb={passed.at[sym, 'pb']:.2f}, "
                            f"de={passed.at[sym, 'de_ratio']:.2f}"),
                ))

        # Held names that no longer pass the screen are sold at review.
        for sym in sorted(held):
            if sym in passing or sym not in data or data[sym].empty:
                continue
            close = float(data[sym].close.iloc[-1])
            if close <= 0:
                continue
            signals.append(Signal(
                strategy_id=self.strategy_id,
                signal_type=SignalType.EXIT,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                product_type=ProductType.CNC,
                reason="thesis break / left screen",
            ))
        return signals
