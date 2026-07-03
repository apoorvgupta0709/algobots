"""3.6 GARP — Growth at a Reasonable Price.

Growth investing with an adult supervising the multiple. The edge: buy
earnings compounders (EPS growth 15%+) only while the market still charges a
reasonable price for that growth — PEG (P/E / growth) below ~1.2 — with low
leverage and revenue growth confirming the profit line. Entries are timed on
consolidations near highs (buy the base, not the breakout); exits fire when
the PEG stretches beyond ~2 or growth decelerates.

Regime: works best on mid-caps graduating to large-caps — businesses whose
growth phase outlasts the market's willingness to pay up for it.

Primary risk: forecast error — the G in PEG is an estimate, and Indian
management guidance is optimistic by habit; a growth miss compresses both the
E and the multiple at once.

India note: cross-check earnings growth against cash conversion (the
revenue_growth > 8 filter is the proxy here) — profit growth without revenue
growth is an accounting story, not a business one.

NOTE: the shipped ``config/fundamentals.csv`` is SYNTHETIC PLACEHOLDER data.
Replace it with a real fundamentals feed before using this screen in
production. The strategy degrades to no signals on an empty frame.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.fundamentals import CsvFundamentals, FundamentalsProvider, screen


class GARPStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt06_garp",
        name="GARP — Growth at a Reasonable Price",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=70,
        params={
            "eps_growth_min": 15.0,        # earnings growth floor (%)
            "peg_max": 1.2,                # entry PEG ceiling (P/E / EPS growth)
            "de_max": 0.5,                 # leverage ceiling
            "revenue_growth_min": 8.0,     # cash-conversion proxy (%)
            "consolidation_band_pct": 10.0,  # buy within this % of the 60d high...
            "fresh_high_lookback": 20,       # ...but not on a fresh 20d high
            "high_lookback": 60,
            "max_names": 8,                # weight = 1/max_names per entry
            "peg_exit": 2.0,               # exit: multiple stretched
            "eps_growth_exit": 8.0,        # exit: growth deceleration (%)
        },
        capital_required=300_000,
        max_positions=1000,                # accumulation sleeve: engine never caps
        max_trades_per_day=25,
        intraday_squareoff=False,
        description=("Monthly GARP screen: EPS growth >15% at PEG <1.2 with D/E "
                     "<0.5 and revenue growth >8% confirming cash conversion. "
                     "Buys consolidations near 60-day highs (not fresh breakouts); "
                     "exits when PEG stretches past ~2 or growth decelerates. "
                     "Needs mid-caps graduating to large-caps; risk is forecast "
                     "error in the G — Indian guidance is optimistic by habit."),
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        # Constructor I/O is allowed; signal-time I/O is not. ctx.extras may
        # override this with a live provider at scan time.
        self._fundamentals: FundamentalsProvider = CsvFundamentals()

    # ------------------------------------------------------------------ helpers
    def _fund_frame(self, ctx: StrategyContext, symbols: list[str]) -> pd.DataFrame:
        provider = ctx.extras.get("fundamentals") or self._fundamentals
        return provider.get(symbols)

    def _garp_screen(self, funds: pd.DataFrame) -> pd.DataFrame:
        """Fundamental gate: growth, leverage and revenue filters, then PEG."""
        passed = screen(funds, {
            "eps_growth": (">", float(self.params["eps_growth_min"])),
            "de_ratio": ("<", float(self.params["de_max"])),
            "revenue_growth": (">", float(self.params["revenue_growth_min"])),
        })
        if passed.empty:
            return passed
        # PEG guard: eps_growth > 0 is implied by the growth floor, but keep
        # the explicit guard so a lowered eps_growth_min cannot divide by <= 0.
        growth = passed["eps_growth"]
        peg = passed["pe"] / growth
        return passed[(growth > 0) & peg.notna() & (peg < float(self.params["peg_max"]))]

    def _in_consolidation(self, df: pd.DataFrame) -> bool:
        """Within the band below the 60d high, but NOT printing a fresh 20d high."""
        hi_n = int(self.params["high_lookback"])
        fresh_n = int(self.params["fresh_high_lookback"])
        if len(df) < hi_n + 1:
            return False
        close = float(df.close.iloc[-1])
        high_60 = float(df.high.rolling(hi_n).max().iloc[-1])
        if high_60 <= 0:
            return False
        band = float(self.params["consolidation_band_pct"])
        near_high = close >= high_60 * (1.0 - band / 100.0)
        prior_20_high = float(df.high.iloc[-(fresh_n + 1):-1].max())
        fresh_20d_high = float(df.high.iloc[-1]) >= prior_20_high
        return near_high and not fresh_20d_high

    # ------------------------------------------------------------------ signals
    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        symbols = list(data.keys())
        if not symbols:
            return []
        funds = self._fund_frame(ctx, symbols)
        if funds.empty or funds.dropna(how="all").empty:
            return []                       # degrade gracefully on no fundamentals

        signals: list[Signal] = []
        held = {p.symbol for p in ctx.open_positions}
        passed = self._garp_screen(funds)
        weight = 1.0 / float(self.params["max_names"])

        # ---- exits at review: multiple stretched or growth decelerating
        for sym in sorted(held):
            df = data.get(sym)
            if df is None or df.empty or sym not in funds.index:
                continue
            row = funds.loc[sym]
            eps_growth = row["eps_growth"]
            pe = row["pe"]
            if pd.isna(eps_growth) or pd.isna(pe):
                continue                    # no data is not an exit reason
            close = float(df.close.iloc[-1])
            peg = pe / eps_growth if eps_growth > 0 else None
            if peg is not None and peg > float(self.params["peg_exit"]):
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    product_type=ProductType.CNC,
                    reason=f"multiple stretched: PEG {peg:.2f} > {self.params['peg_exit']}"))
            elif float(eps_growth) < float(self.params["eps_growth_exit"]):
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    product_type=ProductType.CNC,
                    reason=(f"growth deceleration: EPS growth {float(eps_growth):.1f}% "
                            f"< {self.params['eps_growth_exit']}%")))

        # ---- entries: screened, unheld, and consolidating near the 60d high
        for sym in passed.index:
            if sym in held:
                continue
            df = data.get(sym)
            if df is None or len(df) < self.meta.warmup_bars:
                continue
            if not self._in_consolidation(df):
                continue
            close = float(df.close.iloc[-1])
            row = passed.loc[sym]
            peg = float(row["pe"]) / float(row["eps_growth"])
            signals.append(Signal(
                strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                instrument=sym, timestamp=ctx.now, reference_price=close,
                size_hint=SizeHint(weight=weight),
                product_type=ProductType.CNC,
                reason=(f"GARP entry on consolidation: EPS growth "
                        f"{float(row['eps_growth']):.1f}%, PEG {peg:.2f}, "
                        f"D/E {float(row['de_ratio']):.2f}")))
        return signals
