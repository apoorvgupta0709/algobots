# Strategy Authoring Guide

The contract for adding a strategy to this platform. Follow it exactly — a
parametrized test suite (`tests/test_contract.py`) enforces most of it, and the
backtester/engine assume the rest.

## The rules

1. **One file per strategy** under `algobot/strategies/<category>/`, containing
   exactly one `StrategyBase` subclass. `meta.strategy_id` **must equal the file
   name** (e.g. `id05_gap.py` → `strategy_id="id05_gap"`). Registration is
   automatic — edit no other file.
2. `generate_signals(data, ctx)` is a **pure function**: no network, no file
   I/O, no DB, no broker, deterministic for identical inputs. All state you
   need must be derivable from `data` and `ctx`.
3. **The last row of every DataFrame is the just-closed bar.** Never assume a
   forming bar. Never use rows "after" the signal bar (there are none — but
   also never `shift(-1)`, never center rolling windows).
4. **Strategies never size positions.** Emit `Signal`s; the risk engine sizes
   from stop distance (0.5–1% of capital). Every ENTRY signal must carry at
   least one of: `stop_loss` (on the **underlying**), a defined-risk
   `structure`, or a `size_hint` (portfolio strategies only).
5. **Don't reimplement central risk management.** Breakeven at +0.8R, the 60%
   MFE ratchet trail, intraday square-off at 15:15, daily/weekly loss caps and
   the kill switch are engine-level. Your job: entries, regime filters, and
   strategy-specific exits (indicator flips, time stops, targets).
6. Check `ctx.has_open_position` / `ctx.open_positions` to avoid pyramiding
   unless the strategy explicitly pyramids. Emit `SignalType.EXIT` for open
   positions when your exit condition hits (the engine closes at next bar open).

## Skeleton

```python
from algobot.core.enums import Category, ProductType, SignalType, Timeframe
from algobot.core.models import Signal
from algobot.core.strategy import SCAN_EOD, StrategyBase, StrategyContext, StrategyMeta
from algobot.indicators.trend import ema


class MyStrategy(StrategyBase):
    meta = StrategyMeta(
        strategy_id="sw99_example",          # == file name
        name="Human-Readable Name",
        category=Category.SWING,
        timeframe=Timeframe.DAY,             # MIN5 | MIN15 | HOUR1 | DAY
        scan_schedule=SCAN_EOD,              # see "Scan schedules" below
        instruments=["NIFTY50_UNIVERSE"],    # universe key or concrete symbols
        warmup_bars=60,                      # min history before first signal
        params={"fast": 20, "slow": 50},     # tunables (overridable via config)
        capital_required=150_000,            # incl. margin for short options
        max_positions=3,
        max_trades_per_day=3,
        intraday_squareoff=False,            # True for intraday strategies
        is_multi_leg=False,                  # True when emitting OptionStructures
        description="Edge, regime it needs, primary risk (2-3 lines).",
    )

    def generate_signals(self, data, ctx: StrategyContext) -> list[Signal]:
        signals = []
        for sym, df in data.items():
            if len(df) < self.meta.warmup_bars:
                continue
            close = float(df.close.iloc[-1])
            ...
        return signals
```

## Scan schedules (`algobot.core.strategy`)

| Token | When the engine calls you | Data timeframe |
|---|---|---|
| `SCAN_EVERY_5MIN` | every closed 5-min bar, 09:20–15:10 | MIN5 |
| `SCAN_EVERY_15MIN` | every closed 15-min bar | MIN15 |
| `SCAN_0920_ONCE` | once at 09:20 (gap plays, 9:20 straddle, weekly entries) | MIN5 or DAY |
| `SCAN_EOD` | ~15:45 daily (swing/positional/screens) | DAY |
| `SCAN_WEEKLY` | last trading day of the week | DAY |
| `SCAN_MONTHLY` | first trading day of the month (SIP, factors, rebalance) | DAY |
| `SCAN_EXPIRY_DAY` | 5-min cadence, weekly-expiry sessions only | MIN5 |

## Universes (`algobot/core/universes.py`)

`NIFTY_INDEX`, `BANKNIFTY_INDEX`, `INDEX_UNIVERSE`, `NIFTY50_UNIVERSE` (20
liquid large caps), `ETF_UNIVERSE`, `SECTOR_UNIVERSE`, `PAIR_UNIVERSE` — or
concrete Fyers symbols (`NSE:RELIANCE-EQ`). Override `universe(ctx)` for
dynamic universes (relative-strength ranks, screens).

## Options strategies

Attach an `OptionStructure` to the signal; the LegBuilder resolves strikes and
expiries at execution time — identical code paths in backtest, paper and live.

```python
from algobot.core.models import ExpiryRule, StrikeRule
from algobot.options.structures import (
    long_option, straddle, strangle, vertical_spread, iron_condor,
    iron_butterfly, covered_call, calendar, diagonal_pmcc, ratio_backspread)

structure = iron_condor("NSE:NIFTY50-INDEX", short_delta=0.20, wing_steps=4)
structure = vertical_spread(sym, OptionType.CE, "debit",
                            buy_rule=StrikeRule.atm(0), sell_rule=StrikeRule.atm(4),
                            expiry_rule=ExpiryRule.weekly())
```

`StrikeRule`: `.atm(offset_steps)`, `.delta(0.30)`, `.premium_pct(x)`,
`.absolute(strike)`, `.pct_otm(pct)` (plus internal `"rel"` used by condor wings).
`ExpiryRule`: `.weekly(n)`, `.monthly(n)` — Bank Nifty weeklies are gone; the
expiry calendar resolves BANKNIFTY "weekly" to the monthly automatically.

- ENTRY direction for short-vol structures: use `ENTRY_SHORT` with
  `stop_loss` above (e.g. short call strike) and `take_profit` below — both on
  the underlying; the monitor exits the whole structure on a breach.
- Futures legs: `OptionType.FUT` with `StrikeRule.absolute(0)` (see
  `fu01_trend_positional.py`).
- `ctx.option_chain(sym)` gives a chain (synthetic in backtests) for
  strike/delta/premium lookups; `ctx.leg_builder` resolves structures if you
  need resolved strikes inside the strategy (rare).

## Pair strategies

Set `is_pair=True`, emit the first leg as the Signal instrument and the second
via `pair_leg=PairLeg(symbol, hedge_ratio)`. (Backtester support is limited —
document any simplification in the strategy docstring.)

## Portfolio / accumulation strategies (SIP, rebalance, factor sleeves)

Emit `SignalType.REBALANCE` with `size_hint=SizeHint(notional=...)` (or
`weight=`/`qty=`), `product_type=ProductType.CNC`. No stop needed. Round-trip
trade counts stay at zero — the gate evaluates these on backtest months and
equity behaviour instead.

## Fundamentals-driven screens

`algobot.indicators.fundamentals.CsvFundamentals` +
`screen(df, {"pe": ("<", 14), "roce": (">", 18)})`. The shipped
`config/fundamentals.csv` is **synthetic placeholder data** — the strategy
must work whatever rows exist and degrade to no signals on an empty frame.
Access it via `ctx.extras.get("fundamentals")` when provided, else instantiate
`CsvFundamentals()` **in `__init__`** (constructor I/O is allowed; signal-time
I/O is not).

## Reference implementations (read before writing)

| File | Demonstrates |
|---|---|
| `strategies/intraday/id01_orb.py` | intraday 5-min, session slicing, debit-spread execution, one-shot breakout detection |
| `strategies/swing/sw04_supertrend_adx.py` | EOD multi-symbol scan, indicator entry/exit, CNC product |
| `strategies/longterm/lt02_sip.py` | monthly schedule, REBALANCE + SizeHint accumulation |
| `strategies/options/op06_iron_condor.py` | multi-leg structure, regime + expiry-cycle filters, chain lookups, underlying-level stops |
| `strategies/futures/fu01_trend_positional.py` | futures leg via structure, MARGIN product, long/short with flips |

## Definition of done

1. `python -m pytest tests/test_contract.py -q` — green (your strategy is
   picked up automatically).
2. `python scripts/run_backtest.py --strategy <id> --days 250` — runs without
   exceptions, produces sane trades (or accumulation equity for portfolio
   strategies), and persists a run.
3. Docstring at the top of the file: compendium section number, the edge, the
   regime it needs, its primary risk, and any India-specific note.
4. No edits outside your strategy file (and its optional test).
