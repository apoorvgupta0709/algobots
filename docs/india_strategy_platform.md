# India Strategy Platform (Paper / Research Only)

A Python-first research platform for a broad universe of Indian-market strategies —
NSE index options, cash equities/ETFs, and long-term investment allocation studies —
with a one-month paper-qualification workflow and a read-only dashboard.

> **Safety first.** This platform is paper/research only. It places **no** live
> orders, contains **no** broker order-placement / modification / cancellation /
> exit code, and reads **no** secrets. The only "live" concept is the governance
> label `live_eligible_requires_manual_approval`, which a human grants explicitly
> and which still enables nothing. See [Safety invariants](#safety-invariants).

## Components

| Layer | File | Responsibility |
| --- | --- | --- |
| Registry | `config/strategy_universe_india.json` | Declarative strategy universe (metadata only). |
| Registry loader | `scripts/strategy_registry.py` | Typed dataclasses/enums; load + validate + enforce safety rails. |
| Qualification | `scripts/strategy_qualification.py` | Decimal-safe metrics, one-month trial evaluator, Markdown/CSV reports, manual approval. |
| Dashboard | `dashboard/strategy_platform_dashboard.py` | Read-only Streamlit monitor across three desks with a safety panel. |
| Tests | `tests/test_strategy_registry.py`, `tests/test_strategy_qualification.py`, `tests/test_strategy_platform_dashboard.py` | Safety rails, metric correctness, dashboard adapters. |

Data flow:

```
strategy_universe_india.json
        │  load_registry() → validate_strategy() (safety rails)
        ▼
StrategyUniverse ──────────────► dashboard (desks, explainability, safety panel)
        │                                   ▲
        │ paper-trade rows (read-only)      │ metrics + trial verdict
        ▼                                   │
strategy_qualification.compute_metrics ─────┘
        │  evaluate_trial() → recommend (ceiling = QUALIFIED)
        ▼
grant_live_eligibility()  ── manual, explicit, enables no orders ──►  live_eligible_requires_manual_approval (label only)
```

## Strategy universe

Every strategy is one entry in `config/strategy_universe_india.json` with a fixed,
typed schema (see `StrategyDefinition` / `StrategyRisk` in `scripts/strategy_registry.py`).
Strategies are grouped into three **desks**:

- **Options Desk** — NSE index options (NIFTY / BANKNIFTY / FINNIFTY). Executable
  strategies are long-only or debit-defined-risk (single leg or debit spread).
  Families: debit spread, option ORB, CPR+VWAP trend-day, expiry-day gamma,
  option VWAP mean-reversion, plus **scorecard-only** volatility/regime filters and
  straddle / strangle / iron-condor / ratio studies.
- **Equities Desk** — NSE cash-equity and ETF intraday/swing. Families: ORB retest,
  VWAP trend, VWAP mean-reversion, gap-up continuation / gap-down fade, CPR trend-day,
  sector relative-strength, volume shock, single-stock momentum with index
  confirmation, large-cap mean-reversion, ETF momentum, market-neutral divergence
  pairs, plus a scorecard-only market-breadth regime filter.
- **Investment Desk** — positional/long-term. Families: 50/200 DMA momentum, index
  200-DMA regime timing, SIP baseline (the buy-and-hold benchmark), plus
  scorecard-only quality/value/growth composite, sector rotation, risk parity,
  drawdown-controlled allocation, and an overfitting/robustness gate.

### Executable vs scorecard-only

- **Executable** strategies may emit paper trades. They must be long / debit /
  defined-risk (`structure ∈ {single_leg, debit_spread, portfolio, none}`,
  `direction ∈ {long, directional, long_short, market_neutral}`,
  `option_selling = false`) and carry a `risk` block.
- **Scorecard-only** strategies (`executable: false`, `risk: null`) are research
  studies that never generate trades. **All** short-premium / undefined-risk
  structures (short options, straddle, strangle, iron condor, ratio) are forced into
  this bucket — the loader rejects any attempt to make them executable.

### Lifecycle

```
research_candidate → backtest_ready → backtested → paper_enabled
    → paper_observing → qualified → live_eligible_requires_manual_approval
```

Progression is one step at a time. The registry file never ships a strategy at the
terminal `live_eligible_requires_manual_approval` status; it is reachable only via an
explicit manual approval after `qualified` (see below).

## Backtest methodology & cost assumptions

Backtests in this repo are **proxy** backtests: index/underlying-move proxies for
option P&L, not full option-chain replays (consistent with the existing BankNifty and
NSE-pack runners). Treat results as **directional, not exact**. When modelling costs,
account for the real drag on Indian intraday/positional trading:

- **STT** on options (on premium for buys; higher on the sell/exercise side),
- **brokerage** (flat per order) + **exchange transaction charges**, **GST**,
  **SEBI** turnover fees, and **stamp duty**,
- **slippage / spread** — especially on far-OTM options, thin ETFs, and near expiry,
- **circuit limits** and liquidity gaps on single stocks,
- **theta/IV drag** for options held intraday, and IV-crush around events.

Executable strategies inherit the repo's rupee risk envelope by default (₹50,000
paper capital, ₹1,500 max loss/trade, ₹5,000 daily loss cap, ₹40,000–₹50,000 exposure,
1–3 trades/day, 1–5 open positions per strategy) — see each entry's `risk` block.

## Paper trial → manual live eligibility

The qualification engine turns paper-trade rows into a verdict.

1. **Metrics** (`compute_metrics`) — all money is `Decimal` (`ROUND_HALF_UP`,
   quantized to `0.01`), all timestamps IST-aware. Computes closed/open counts,
   win rate, gross profit/loss, net P&L, avg win/loss, profit factor, expectancy,
   largest win/loss, max drawdown (on the cumulative realized-equity curve), max
   consecutive losses, and trading days.
2. **One-month trial** (`evaluate_trial` + `TrialWindow.one_month_from`) — filters
   trades whose exit falls in `[start, start + 1 calendar month)` and scores them
   against `QualificationCriteria`:

   | Criterion | Default |
   | --- | --- |
   | `min_closed_trades` | 15 |
   | `min_trading_days` | 10 |
   | `min_win_rate` | 40% |
   | `min_profit_factor` | 1.20 |
   | `min_net_pnl` | ₹0 |
   | `max_drawdown` | ₹6,000 |
   | `min_expectancy` | ₹0 |

   Criteria are overridable via `--criteria <json>`.
3. **Recommendation** — a passing trial recommends advancing toward `qualified`.
   **The recommendation ceiling is `qualified`; the evaluator never recommends or
   auto-assigns a live status.**
4. **Manual live eligibility** (`grant_live_eligibility`) — the only path to
   `live_eligible_requires_manual_approval`. It requires **all** of: the strategy is
   executable and currently `qualified`, a named human approver, the exact
   confirmation phrase `APPROVE LIVE ELIGIBILITY <strategy_id>`, and an explicit
   acknowledgement. Even on success it returns a governance record only — **no order
   code is enabled anywhere.**

## Commands

```bash
# Validate / summarize the strategy universe
uv run python scripts/strategy_registry.py --validate --summary

# One-month paper qualification on the bundled sample (no DB needed)
uv run python scripts/strategy_qualification.py --mode sample --out reports/india_qualification.md

# ... from a read-only JSON/CSV export of paper trades
uv run python scripts/strategy_qualification.py --mode file --input trades.json --from 2026-06-01

# Manual live-eligibility grant (label only; enables no orders)
uv run python scripts/strategy_qualification.py --grant-live-eligibility \
  --strategy-id sip_baseline_buy_and_hold --approved-by "Apoorv" \
  --confirm "APPROVE LIVE ELIGIBILITY sip_baseline_buy_and_hold" --acknowledge

# Read-only dashboard (loopback)
uv run streamlit run dashboard/strategy_platform_dashboard.py \
  --server.address 127.0.0.1 --server.port 8502

# Tests
uv run pytest tests/test_strategy_registry.py tests/test_strategy_qualification.py \
  tests/test_strategy_platform_dashboard.py -q
```

## Dashboard

Three tabs mirror the desks (consolidated **Options Desk**, **Equities Desk**,
**Investment Desk**) plus a **Safety** tab. For each strategy it shows lifecycle
status, one-month trial metrics + PASS/FAIL, and explainability (what it does / when
it enters / when it exits / filters / rationale). The dashboard:

- runs SELECT-only queries through `assert_readonly_sql` on a read-only connection
  (prefers the least-privilege `dashboard_ro` role),
- **degrades gracefully** to bundled sample data when the DB is unavailable and says
  which source it is using,
- makes **no** broker/FYERS calls and **no** LLM/network calls,
- binds to loopback by default.

## Safety invariants

- Registry top-level and every strategy: `paper_only: true`, `live_orders_enabled: false`
  (validated with strict JSON-boolean parsing — `"false"` strings are rejected).
- Short-premium / undefined-risk structures are always scorecard-only (non-executable).
- Executable strategies are long / debit / defined-risk only.
- Qualification never auto-promotes past `qualified`; live eligibility is manual and
  label-only.
- No file in this platform imports a broker order API or writes to `trading.execution_log`.

## Limitations

- **Proxy backtests only** — no full option-chain replay; numbers are directional.
- The qualification engine consumes paper-trade rows; it does not itself run
  strategies or ingest market data.
- Metric thresholds are heuristics, not guarantees of forward performance; the
  overfitting/robustness scorecard is a reminder, not an automated gate yet.
- Portfolio/allocation studies (risk parity, sector rotation, drawdown control) are
  scorecard-only until portfolio accounting and rebalance-cost modelling exist.
- Nothing here is investment advice; it is a research scaffold.
