# action.md — Algobots Roadmap: Backtest → Paper → Live

> Audit date: 2026-07-07 · Branch audited: `main` (72 commits, all feature work
> merged via `feat/*` branches, latest activity 2026-07-03)
>
> Goal: a full algo-trading system — backtesting, paper trading, and (eventually,
> explicitly gated) live trading — for Indian markets via FYERS API v3.

---

## 1. Where the repo actually is (git + code audit)

### 1.1 Two generations coexist in one repo

**Generation 1 — legacy research system (`scripts/`, `dashboard/`, `migrations/`)**
- Paper-only BankNifty/Nifty options research: `scripts/banknifty_options_paper.py`
  (tick engine), NSE intraday 5-strategy pack, trend-pattern library, knowledge
  library (pgvector), watchlist reports, read-only Streamlit dashboard.
- Backtests here are **proxy backtests** (index-move P&L), not option-chain replay.
- Hard safety invariants (CLAUDE.md): `paper_only: true` enforced by config
  validation, no order-placement code, SELECT-only dashboard, `execution_log`
  requires human `approval_id`.
- 17 PostgreSQL migrations, ~45 test files, cron shell wrappers.

**Generation 2 — the `algobot/` platform (rebuilt 2026-07, "phase 0–2 rebuild" commits)**
This is already most of the target system:

| Layer | Module | State |
|---|---|---|
| Core contract | `algobot/core/` (models, enums, strategy ABC, registry, config, IST clock) | Built; same `Signal`/`Order`/`Position` vocabulary across backtest/paper/live |
| Data | `algobot/data/` (Fyers feed, parquet cache, NSE expiry calendar, corporate actions, instruments) | Built |
| Indicators/options | `algobot/indicators/`, `algobot/options/` | Built (25 tests) |
| Costs | `algobot/costs/india.py` — full Indian cost stack (STT, exchange, GST, stamp, slippage) | Built; shared by backtest, paper, live |
| Backtest | `algobot/backtest/` — event-driven replay, cost-model fills, portfolio, metrics, report | Built (16 tests); option-chain replay limited by data (see §2) |
| Brokers | `algobot/broker/paper.py` (simulated fills + cost stack) and `algobot/broker/fyers/broker.py` (**live adapter exists**, lazy client, test-injectable) | Built, live path never exercised with real money |
| Execution | `algobot/execution/` — OrderManager, RiskEngine (per-trade sizing, daily −2.5% / weekly −5% caps, max positions/trades, **kill switch**), position monitor, square-off | Built |
| Engine | `algobot/engine/` — runner, scheduler, lifecycle (`set_mode`), **paper-to-live gate** (`gate.py` + `config/gate.yaml`: PF ≥ 1.3 after costs, ≥ 60 paper trades or ≥ 6 OOS months, DD ≤ 15%, stop-fire fidelity ≤ 0.5%) | Built; gate flips *eligibility* only, promotion is an explicit human action |
| Strategies | 49 strategies: 8 intraday, 10 swing, 15 options, 3 futures, 13 long-term | Landed with contract tests + backtests |
| API/UI | `algobot/api/` (FastAPI: reads, controls, job queue), `algobot/dashboard/` | Built (25 tests) |
| Ops | `Dockerfile`, `docker-compose.yml` (db / engine / api / dashboard) | Built, not proven in sustained operation |

### 1.2 Git workflow observations

- **Pattern:** feature branches (`feat/…`) merged into `main`; parallel agents
  landed strategy batches with per-batch commits. Reasonable, but:
- **No CI.** `.github/workflows/` does not exist. Tests (`uv run pytest -q`)
  run only when someone remembers. For a system heading to live orders this is
  the single biggest process gap.
- **WIP commits on main** ("WIP: phase-2 rebuild agents landing…") — fine for
  research, not fine once real money is downstream of `main`.
- **No tags/releases** — no way to say "this exact code was live on date X".
- **Doc drift:** CLAUDE.md still says "No FYERS order-placement code exists",
  but `algobot/broker/fyers/broker.py` is a live order adapter (gated, unused).
  The invariant must be *restated* (live path exists but is triple-gated), not
  silently contradicted.

### 1.3 Bottom line

You do **not** need to build a backtest/paper/live platform — you already have
one. The roadmap below is about **hardening, validating, unifying, and then
carefully switching on** what exists, in that order.

---

## 2. Gap analysis

**Backtesting**
- G1. No historical option-chain data → options backtests are synthetic/proxy
  (gate already discounts them 50% via `synthetic_backtest_discount`). Need a
  growing store of real chain snapshots (ingestion exists: `013_options_chain_snapshots`,
  `scripts/ingest_fyers_optionchain.py`) plus an external historical source.
- G2. No systematic walk-forward / out-of-sample discipline baked into the
  runner — OOS months are a gate input but the split isn't enforced tooling-side.
- G3. Fill realism: paper broker fills market orders at quote ± slippage model;
  no depth/latency modelling, no partial fills. Acceptable for index options at
  1–2 lots; must be revisited before size.

**Paper trading**
- G4. The `algobot` engine hasn't run unattended through full market weeks —
  crash recovery, token expiry mid-session, feed gaps, restart-with-open-positions
  are untested in anger.
- G5. Two systems produce paper results (legacy scripts + algobot platform);
  metrics aren't comparable and effort is split.
- G6. Monitoring/alerting: the legacy pre-market readiness watchdog exists, but
  the platform has no "engine died / feed stale / position unmonitored" alerting.

**Live trading**
- G7. Live order lifecycle: rejections, partial fills, modify-order, exchange
  freeze-quantity limits, broker downtime — the FyersBroker is a thin adapter;
  the failure matrix is unhandled/untested.
- G8. State reconciliation: on restart, DB positions vs. actual FYERS positions
  must be reconciled before the engine acts. No reconciler exists.
- G9. Auth robustness: daily FYERS token refresh (08:45 scheduled) has no
  failure escalation path.
- G10. No CI, no release tagging, no config-freeze discipline (G-process).
- G11. CLAUDE.md safety invariants must be formally amended (human sign-off)
  before any live enablement — today config validation *rejects*
  `live_orders_enabled: true` in the legacy configs; the platform's equivalent
  guard needs the same test coverage.

---

## 3. Roadmap

Each phase has an **exit gate**. Do not start the next phase until the gate is met.
Money at risk stays **zero until Phase 5**, and minimal (1 lot) until Phase 6.

### Phase 0 — Engineering hygiene (Week 1) 🔧
1. **CI**: GitHub Actions running `uv sync --group dev && uv run pytest -q` on
   every PR and push to `main`. Branch protection: no direct pushes, PRs need
   green CI.
2. **Repo policy**: no WIP commits on `main`; tag releases (`v0.x`); CHANGELOG.
3. **Doc truth**: update CLAUDE.md — state that a live adapter exists and is
   inert behind (a) strategy `mode != live`, (b) gate eligibility, (c) explicit
   human `set_mode(..., LIVE)`; add a test asserting the default-config engine
   can never construct a live broker.
4. **Decide the consolidation** (see §4): `algobot/` is the platform of record;
   legacy `scripts/` continue as data-ingestion + research utilities only.

**Exit gate:** CI green and required; safety tests cover the live-path locks.

### Phase 1 — Data foundation (Weeks 1–3) 📊
1. Run option-chain snapshot ingestion (`ingest_fyers_optionchain.py`) on cron
   every trading day — real chain history compounds from now; every day not
   ingesting is lost forever.
2. Backfill index/equity candles for the full strategy universe; nightly
   `compute_technical_factors`.
3. Data-quality checks: gap detection, corporate-action verification, expiry
   calendar vs. NSE announcements (Tuesday migration already handled — keep a test).
4. Extend the legacy readiness watchdog to cover the platform's parquet cache + DB.

**Exit gate:** 100% trading-day chain-snapshot coverage for ≥ 2 weeks; watchdog
alerts on any gap.

### Phase 2 — Backtest credibility (Weeks 2–6) 🧪
1. Wire real chain snapshots into `algobot/backtest/option_data.py` so recent
   option backtests use actual premiums; keep synthetic pricing (clearly
   flagged) only for pre-history.
2. Add a walk-forward harness: train/validation/OOS splits enforced by tooling,
   results written to `backtest_runs` with the split labelled — the gate should
   only count genuinely OOS months.
3. Cost-model calibration: compare modeled costs vs. FYERS contract notes /
   published charges; assert in tests.
4. Run all 49 strategies through the harness; **cull ruthlessly** — expect a
   minority to survive after full costs. Rank survivors; shortlist ≤ 10 for paper.
5. Explicitly exclude short-premium strategies (op04/op05/op06/op07/op09…) from
   any live shortlist until margin, assignment and tail-risk handling exist —
   they violate the current "long options only" risk stance.

**Exit gate:** shortlist of ≤ 10 strategies, each with cost-inclusive OOS PF ≥ 1.3
and DD ≤ 15% on the walk-forward harness.

### Phase 3 — Paper trading at production quality (Weeks 4–10) 📝
1. Run the docker-compose stack (db/engine/api/dashboard) daily through full
   sessions with the shortlist in `mode: paper`.
2. Harden operations: supervised restarts, restart-with-open-positions recovery,
   feed-stale detection, token-refresh failure escalation (G4, G6, G9).
3. Alerting: engine heartbeat + risk events (kill switch, loss-cap hits) pushed
   to you (email/Telegram) — not just journaled.
4. Execution honesty: nightly compare paper stop exits vs. modeled stops
   (feeds the gate's `stop_fire_tolerance_pct`).
5. Weekly review ritual: paper P&L vs. backtest expectation per strategy;
   investigate divergence > 1σ before anything else.

**Exit gate:** ≥ 6 consecutive weeks of unattended paper operation with zero
unexplained crashes/data gaps, and ≥ 60 paper trades on at least 2 strategies
tracking their backtest expectation.

### Phase 4 — Live-readiness engineering (Weeks 8–12, zero money) 🔩
1. **Order lifecycle hardening** (G7): handle reject/partial-fill/cancel-race;
   freeze-qty splitting; idempotent submission (client order IDs); retry policy
   with a hard "give up and alert" state.
2. **Reconciler** (G8): on startup and every N minutes, diff DB open positions
   vs. FYERS positions/orderbook; halt trading + alert on mismatch.
3. **Kill-switch surface**: one command/API call flattens everything and
   disables entries (RiskEngine kill switch already exists — add the flatten
   hook and a dashboard button + CLI).
4. **Dry-run against live API**: place → immediately cancel far-from-market
   limit orders in a FYERS **sandbox or 1-share equity test**, verifying the
   adapter end-to-end without market risk (explicit human approval for this step).
5. Failure-mode drills: kill the engine mid-position (paper), yank the network,
   expire the token — verify recovery each time.
6. Human process: written run-book (start/stop/flatten/reconcile), and amend
   CLAUDE.md invariants with your explicit sign-off (G11).

**Exit gate:** all drills pass; reconciler + kill switch demonstrated; run-book
written; you have signed off the amended safety policy.

### Phase 5 — Live pilot (Month 4+) 🟢
1. Promote **one** gate-eligible strategy via explicit `set_mode(..., LIVE)` —
   long-options or cash-equity only, **1 lot / minimum quantity**, its own
   sub-capital allocation.
2. Tight leash: daily loss cap enforced (−2.5% already in `settings.yaml`),
   auto-kill on cap, human review of every fill for the first 2 weeks.
3. Measure live vs. paper slippage; feed back into the cost model.
4. Run ≥ 1 month. Any unexplained behaviour → back to paper, fix, restart clock.

**Exit gate:** 1 month live, slippage within model, no manual interventions
caused by system faults.

### Phase 6 — Scale deliberately (Month 5+) 📈
- Add strategies one at a time (each individually gate-eligible + 1-month solo pilot).
- Scale size stepwise (1 → 2 → 4 lots), re-checking slippage at each step.
- Portfolio-level risk: correlation caps across strategies, aggregate exposure
  limits, weekly capital-allocation review.
- Only now revisit: futures strategies, defined-risk spreads (needs margin
  modelling), and the long-term equity book as a separate low-frequency track.

---

## 4. Consolidation decision (recommended)

Make **`algobot/` the single platform of record** for backtest/paper/live.
The legacy layer keeps three jobs: (1) FYERS data ingestion crons, (2) the
knowledge/research library, (3) the read-only research dashboard. Port the two
proven legacy engines (BankNifty pullback runner-exit logic, intraday pack) as
`algobot` strategies so their track record continues inside one measurement
system. Don't maintain two execution engines.

## 5. Standing safety rules (unchanged by this roadmap)

- Promotion to live is **never automatic** — the gate only flips eligibility;
  a human calls `set_mode`. Keep the `force` path audit-logged and alarmed.
- Short-premium structures stay blocked until Phase 6 prerequisites exist.
- Kill switch and daily/weekly loss caps are non-negotiable and test-covered.
- Every live-enabling change lands via PR with green CI and updates this file.

## 6. Immediate next actions (this week)

1. Add GitHub Actions CI + branch protection (Phase 0.1).
2. Start daily option-chain snapshot cron (Phase 1.1) — data compounds, start now.
3. Fix CLAUDE.md doc drift + add live-path lock tests (Phase 0.3).
4. Pick the consolidation path (§4) and freeze which engine owns paper results.
5. Schedule the first full-universe walk-forward backtest run (Phase 2.4).
