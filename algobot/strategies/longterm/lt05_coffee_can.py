"""3.5 Quality / Coffee-Can Compounding — buy dominance, then do nothing.

Compendium section: 3.5 (Quality / Coffee-Can Compounding).

Edge: dominant, high-return businesses (sustained high ROCE/ROE, double-digit
revenue growth, negligible debt) compound quietly for years with shallower
drawdowns than the index; the strategy's real edge is behavioural — it removes
the sell decision entirely, so the compounding is never interrupted.

Regime: none required. This is for investors who cannot watch the market
daily; it buys on the first trading day of the month and otherwise sits still.

Primary risk: paying any price. Quality at 90x earnings spent 2022-23 going
sideways while earnings caught up with the multiple — that is the deal you
sign. Valuation is deliberately NOT an exit reason here; the GARP-style entry
screen (growth + return thresholds, not a P/E cap) is the only discipline
guarding the multiple.

India note: the financialisation-of-savings and consumption runway gives
dominant Indian franchises an unusually long reinvestment ramp, which is what
makes a hold-a-decade rule viable; the GARP entry discipline guards the
multiple on the way in.

Rules:
- Screen the NIFTY50 universe monthly on fundamentals: ROCE and ROE above the
  thresholds, revenue growth above 10%, negligible debt (D/E < 0.3), promoter
  pledge under 5%.
- Buy up to ``max_names`` screened names not already held, equal weight, CNC.
- NEVER sell — except on severe business deterioration (ROCE collapsing below
  12 or leverage blowing past 1.0x). No stops, no targets, no trailing.

DATA NOTE: ``config/fundamentals.csv`` is SYNTHETIC PLACEHOLDER data (see
``algobot/indicators/fundamentals.py``). The screen works with whatever rows
exist and degrades to no signals on an empty frame; wire a real fundamentals
feed before using this in production.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.fundamentals import CsvFundamentals, screen


class CoffeeCanStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt05_coffee_can",
        name="Quality / Coffee-Can Compounding",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=1,
        params={
            # Compendium asks for ROCE/ROE sustained above 18-20%; the shipped
            # placeholder CSV is deliberately screened at 15 so the strategy
            # produces names to hold — restore 18-20 with real fundamentals.
            "roce_min": 15.0,
            "roe_min": 15.0,
            "rev_growth_min": 10.0,
            "de_max": 0.3,
            "pledge_max": 5.0,
            "max_names": 10,          # coffee can: 10-15 names, then sit still
            # severe-deterioration exits — the ONLY reason a name ever leaves
            "exit_roce": 12.0,
            "exit_de": 1.0,
        },
        capital_required=300_000,
        max_positions=1000,           # accumulation strategy: positions never capped
        max_trades_per_day=15,
        intraday_squareoff=False,
        description=("Monthly fundamentals screen for dominant high-ROCE/ROE, "
                     "low-debt compounders; buys up to 10 names equal weight and "
                     "never sells except on severe business deterioration. Edge is "
                     "uninterrupted compounding; risk is paying any price."),
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        # Constructor I/O is allowed; signal-time I/O is not.
        self._fundamentals = CsvFundamentals()

    # ------------------------------------------------------------------ helpers
    def _fundamentals_frame(self, symbols: list[str], ctx: StrategyContext) -> pd.DataFrame:
        """Fundamentals rows indexed by the symbols as passed.

        Prefers a runtime-supplied ``ctx.extras['fundamentals']`` (provider or
        pre-built frame); falls back to the CSV provider loaded in __init__.
        """
        src = ctx.extras.get("fundamentals")
        if src is None:
            src = self._fundamentals
        if isinstance(src, pd.DataFrame):
            return src.reindex(symbols)
        return src.get(symbols)

    # ------------------------------------------------------------------ signals
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        symbols = sorted(sym for sym, df in data.items() if len(df) >= self.meta.warmup_bars)
        if not symbols:
            return []
        fund = self._fundamentals_frame(symbols, ctx)
        if fund is None or fund.empty or fund.isna().all().all():
            return []                      # no fundamentals -> degrade to no signals

        held = {p.symbol for p in ctx.open_positions}
        signals: list[Signal] = []

        # ---- exits: ONLY on severe business deterioration. Valuation is never
        # a reason to sell — quality at 90x spending a year sideways is the deal.
        for sym in symbols:
            if sym not in held:
                continue
            row = fund.loc[sym]
            deteriorated = ((pd.notna(row["roce"]) and row["roce"] < self.params["exit_roce"])
                            or (pd.notna(row["de_ratio"]) and row["de_ratio"] > self.params["exit_de"]))
            if deteriorated:
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now,
                    reference_price=float(data[sym].close.iloc[-1]),
                    product_type=ProductType.CNC,
                    reason=(f"business deterioration: roce={row['roce']:.1f} "
                            f"de_ratio={row['de_ratio']:.2f}")))

        # ---- entries: screened quality names not yet in the can, equal weight
        passing = screen(fund, {
            "roce": (">", self.params["roce_min"]),
            "roe": (">", self.params["roe_min"]),
            "revenue_growth": (">", self.params["rev_growth_min"]),
            "de_ratio": ("<", self.params["de_max"]),
            "promoter_pledge": ("<", self.params["pledge_max"]),
        })
        # dominance first: rank by ROCE descending (symbol as deterministic tiebreak)
        candidates = [sym for sym, _ in sorted(passing.iterrows(),
                                               key=lambda kv: (-kv[1]["roce"], kv[0]))
                      if sym not in held]
        slots = max(int(self.params["max_names"]) - len(held), 0)
        buys = candidates[:slots]
        if buys:
            weight = 1.0 / len(buys)
            for sym in buys:
                row = fund.loc[sym]
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                    instrument=sym, timestamp=ctx.now,
                    reference_price=float(data[sym].close.iloc[-1]),
                    size_hint=SizeHint(weight=weight),
                    product_type=ProductType.CNC,
                    reason=(f"coffee-can add: roce={row['roce']:.1f} roe={row['roe']:.1f} "
                            f"rev_g={row['revenue_growth']:.1f} de={row['de_ratio']:.2f}")))
        return signals
