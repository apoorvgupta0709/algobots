"""3.7 Dividend-Yield Investing — get paid to wait, in businesses that can
afford the cheque.

Edge: a healthy dividend is a cash-flow proof the accounts cannot fake. Names
yielding 3-4%+ with a payout ratio under 60%, stable earnings and low debt
compound quietly: the yield pays you to wait and reinvested payouts do the
rest. Exit when the dividend itself deteriorates — a cut (or the derating that
precedes one) is the company telling you the thesis is over.

Regime: shines in sideways and high-rate environments where multiple expansion
stalls and carry dominates returns; behaves as the lower-volatility income
sleeve of the book rather than a beta engine.

Risk: yield traps — a fat trailing yield is often just a crashed price ahead
of a dividend cut, so this screen refuses any name trading more than
``trap_dd_pct`` below its 252-day high. PSU payouts follow government cash
needs, not shareholder economics, and can vanish with a budget.

India note: dividends are taxed at the investor's slab rate — high-bracket
investors are often better served by buyback-prone compounders than by
headline yield. Also note the shipped ``config/fundamentals.csv`` is SYNTHETIC
PLACEHOLDER data; wire a real fundamentals feed before trading this, and the
strategy degrades to no signals when the fundamentals frame is empty.
"""
from __future__ import annotations

import pandas as pd

from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal, SizeHint
from algobot.core.strategy import SCAN_MONTHLY, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.fundamentals import CsvFundamentals, FundamentalsProvider, screen


class DividendYieldStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="lt07_dividend_yield",
        name="Dividend-Yield Investing",
        category=Category.LONGTERM,
        timeframe=Timeframe.DAY,
        scan_schedule=SCAN_MONTHLY,
        instruments=["NIFTY50_UNIVERSE"],
        warmup_bars=260,
        params={
            # Compendium wants 3-4%+ trailing yield. The placeholder CSV's
            # yields for names that also clear the payout/debt/earnings gates
            # top out around 1.6% (higher-yield rows fail payout_ratio < 60 or
            # eps_growth > 0), so the shipped default is relaxed to 1.0 to keep
            # the screen alive on placeholder data. Raise towards 3.0-4.0 once
            # a real fundamentals feed is wired in.
            "yield_min": 1.0,            # min trailing dividend yield, %
            "payout_max": 60.0,          # max payout ratio, % (affordable cheque)
            "de_max": 0.5,               # max debt/equity (low leverage)
            "eps_growth_min": 0.0,       # eps_growth > 0: stable-earnings proxy
            "trap_dd_pct": 30.0,         # yield-trap guard: max % below 252d high
            "exit_yield_min": 1.0,       # exit: yield below this = cut/derating
            "exit_payout_max": 90.0,     # exit: payout above this = unsustainable
            "max_names": 8,              # portfolio breadth cap per review
        },
        capital_required=300_000,
        max_positions=1000,              # accumulation sleeve: engine never caps
        max_trades_per_day=10,
        intraday_squareoff=False,
        description=("Monthly screen for dividend payers that can afford the "
                     "cheque: yield above threshold with payout < 60%, D/E < 0.5 "
                     "and positive EPS growth, plus a yield-trap guard rejecting "
                     "names >30% below their 252-day high. Equal-weight CNC "
                     "accumulation; exits on dividend cut/derating or a payout "
                     "ratio that turns unsustainable."),
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        # Constructor I/O is allowed; signal-time I/O is not. ctx.extras may
        # override this with a runtime-provided fundamentals frame.
        self._fundamentals = CsvFundamentals()

    def generate_signals(self, data: dict[str, pd.DataFrame], ctx: StrategyContext) -> list[Signal]:
        symbols = [sym for sym, df in data.items() if len(df) >= self.meta.warmup_bars]
        if not symbols:
            return []

        fund = ctx.extras.get("fundamentals")
        if fund is None:
            fund = self._fundamentals.get(symbols)
        elif isinstance(fund, FundamentalsProvider):
            fund = fund.get(symbols)
        else:
            fund = fund.reindex(symbols)
        if fund is None or fund.empty or fund.dropna(how="all").empty:
            return []  # degrade gracefully: no fundamentals, no signals

        held = {pos.symbol for pos in ctx.open_positions if pos.qty > 0}
        signals: list[Signal] = []

        # ---------------------------------------------------------- exits first
        # Review held names for dividend deterioration.
        for sym in sorted(held):
            df = data.get(sym)
            if df is None or not len(df):
                continue
            if sym not in fund.index:
                continue
            row = fund.loc[sym]
            dy, payout = row.get("dividend_yield"), row.get("payout_ratio")
            reason = ""
            if pd.notna(dy) and float(dy) < float(self.params["exit_yield_min"]):
                reason = f"dividend cut/derating: yield {float(dy):.2f}% < {self.params['exit_yield_min']}%"
            elif pd.notna(payout) and float(payout) > float(self.params["exit_payout_max"]):
                reason = f"unsustainable cheque: payout {float(payout):.0f}% > {self.params['exit_payout_max']}%"
            if reason:
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.EXIT,
                    instrument=sym, timestamp=ctx.now,
                    reference_price=float(df.close.iloc[-1]),
                    product_type=ProductType.CNC, reason=reason))

        # ---------------------------------------------------------- the screen
        passed = screen(fund, {
            "dividend_yield": (">", float(self.params["yield_min"])),
            "payout_ratio": ("<", float(self.params["payout_max"])),
            "de_ratio": ("<", float(self.params["de_max"])),
            "eps_growth": (">", float(self.params["eps_growth_min"])),
        })

        # Yield-trap guard: a fat yield on a crashed price is usually a cut in
        # waiting — reject anything too far below its 252-day high.
        buys: list[tuple[str, float, float]] = []   # (symbol, close, yield)
        for sym in passed.index:
            if sym in held:
                continue
            df = data[sym]
            close = float(df.close.iloc[-1])
            high_252 = float(df.close.iloc[-252:].max())
            if high_252 <= 0:
                continue
            dd_pct = (high_252 - close) / high_252 * 100.0
            if dd_pct > float(self.params["trap_dd_pct"]):
                continue  # yield trap: price crashed ahead of the dividend
            buys.append((sym, close, float(passed.loc[sym, "dividend_yield"])))

        # Highest yield first, capped at max_names, equal weight across buys.
        buys.sort(key=lambda t: (-t[2], t[0]))
        buys = buys[: int(self.params["max_names"])]
        if buys:
            weight = 1.0 / len(buys)
            for sym, close, dy in buys:
                signals.append(Signal(
                    strategy_id=self.strategy_id, signal_type=SignalType.REBALANCE,
                    instrument=sym, timestamp=ctx.now, reference_price=close,
                    size_hint=SizeHint(weight=weight),
                    product_type=ProductType.CNC,
                    reason=(f"dividend screen: yield {dy:.2f}%, payout < "
                            f"{self.params['payout_max']:.0f}%, D/E < "
                            f"{self.params['de_max']}, EPS growth > 0")))
        return signals
