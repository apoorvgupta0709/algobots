# Critical review: algobots repo (2026-07-08)

_Scope: full repo on branch `claude/algo-trading-code-review-qcv4p4` — both the
legacy `finance-db` research system (`scripts/`, `dashboard/`, `config/`,
`migrations/`) and the new `algobot/` 49-strategy platform. Five parallel
specialist review passes (trading logic & backtests, code correctness,
dashboard/ops, safety/live-order gate, algobot platform realism) plus a direct
audit of project config, tests, and deployment. Reviewer goal, per the owner:
"my aim is to make money in algo trading."_

## Executive summary

**Verdict: the system is well engineered in places, but as of today there is no
demonstrated money-making edge, the paper P&L you watch is structurally
inflated, and the repo's own safety story is no longer true — real, reachable
live-order code now exists in `algobot/` behind an unauthenticated,
network-exposed API.**

Four headline problems, in order of importance to the stated goal:

1. **No validated edge.** The only enabled BankNifty strategy fails 2 of its
   own 4 acceptance gates (expectancy 0.13R vs required 0.15R; MFE capture 33%
   vs required 55%) on the honest post-bugfix backtest; the 5-strategy intraday
   pack is net **-₹4,519** over 4.5 months even after in-sample cherry-picking
   of which legs to keep. Stale pre-bugfix reports showing 86% win rate /
   ₹101k profit still sit in `reports/` unmarked.
2. **Paper P&L is optimistic by construction.** The live paper engine fills at
   LTP (never crossing the spread it explicitly allows, up to ₹5 / 3%), and
   applies **zero** brokerage/STT/fees to `realized_pnl`. The proxy backtests
   model option P&L as a constant linear beta of index moves — no theta, no
   vega/IV crush, no gamma. Every number the dashboard shows is a gross,
   frictionless upper bound.
3. **The paper-only invariant is broken at repo level.** CLAUDE.md says "No
   FYERS order-placement code exists" — false since the `algobot/` platform was
   merged: `algobot/broker/fyers/broker.py:51` places real orders,
   `algobot/broker/fyers/auth.py` does fully headless TOTP+PIN login, and
   `codesfiles/s102_algobotstart.py` is a runnable legacy live bot that also
   **shorts options**. Today it's safe only by configuration (all 49 strategies
   `mode: paper`), not by fail-closed code.
4. **The live gate is bypassable by one HTTP call.** The FastAPI control plane
   has no authentication, `CORS allow_origins=["*"]` with credentials, exposes
   `POST /strategies/{id}/promote {"force": true}` which skips the gate, and
   docker-compose publishes it on `0.0.0.0:8000` (dashboard on `0.0.0.0:8501`,
   default DB password `algobot`). Anyone who can reach port 8000 can put a
   naked short strangle live.

---

## 1. Will this make money? (the evidence says: not yet)

### 1.1 Stale, inflated backtest artifacts (CRITICAL)

`reports/banknifty_pullback_v2_proxy_backtest_20260611_164707.md` and
`..._171900.md` report **86.96% win rate, 1.22R expectancy, ₹101,164 P&L** —
generated *before* the 2026-06-11/12 bug-fix commits (inverted relative-strength
check, dead ORB filter, breakeven/ratchet timing, missing cost line). The same
window re-run on fixed code (`..._20260612_061414.md`) collapses to **77
trades, 61.04% win rate, 0.13R expectancy, ₹13,974** — an ~86% drop. The
exit-tuning JSONs that picked the ratchet parameters now live in
`config/banknifty_options_paper.json:246-252` come from the *inflated* world.
Delete or clearly mark the stale artifacts as superseded, and re-derive the
tuned exit parameters on fixed code.

### 1.2 The flagship strategy fails its own gates yet is enabled (CRITICAL)

`config/banknifty_options_paper.json:130-141` keeps
`banknifty_constituent_led_directional_long_options` at `status: "paper_ready"`,
`enabled: true`, while its own latest report shows FAIL on expectancy
(0.13R < 0.15R) and FAIL on MFE capture (33.13% < 55%). 0.13R over 77 proxy
trades is indistinguishable from noise.

### 1.3 The strategy pack is curve-fit and still net negative (HIGH)

`config/nse_intraday_options_strategy_pack.json` disables `nifty_orb_debit_spread`
and the NIFTY leg of `cpr_trend_debit_spread` *because they lost money in the
same Feb–Jun window used as the evidence base* — in-sample selection. Even after
pruning, the pack backtests to **-₹4,519 over 136 trades**; the two positive
legs have n=28 and n=6 (too small to trust). Zero walk-forward / out-of-sample
evidence exists in `reports/`.

### 1.4 The proxy P&L model omits the dominant real-world drags (HIGH)

`run_banknifty_pullback_v2_backtest.py:304,334`: constant premium (₹1000 ATM /
₹700 OTM1) and constant beta (0.5 / 0.35) for the whole life of every trade.
No theta (holds up to 239 minutes in the trades CSV), no IV crush after the
triggering move, no gamma. Costs are a flat ₹100/₹120/₹250 guess, not derived
from the real NSE brokerage+STT+GST+exchange schedule — for 4-order debit-spread
round trips that is plausibly an underestimate, which alone could erase the
thin positive legs.

### 1.5 The live paper engine's P&L is gross and LTP-filled (HIGH)

Entries and exits record LTP (`banknifty_options_paper.py:3032,3173,3328`),
never bid/ask, while the spread filter *permits* up to ₹5 / 3% spreads —
₹150–300/lot round trip of un-modeled cost. No brokerage/STT is deducted from
`research.option_paper_trades.realized_pnl` anywhere. The dashboard number you
watch daily is an upper bound, before theta.

### 1.6 Other edge-relevant defects

- **Stale quote = unprotected position** (HIGH): if quotes go stale >90s, exit
  evaluation is skipped entirely until feed recovery or 15:20 force-exit
  (`banknifty_options_paper.py:3356-3384`). All stops are polling-loop stops,
  not exchange-resident orders — a materially weaker guarantee if this logic
  ever goes live.
- **Daily-loss cap counts realized P&L only** (MEDIUM): the ₹5,000 cap holds
  today only because 3×₹1,500=₹4,500 happens to fit; a gap/outage loss beyond
  the sized stop, or a config bump, breaks the "ceiling."
- **Beta-estimated stops** (MEDIUM): sizing/stop compliance depends on a noisy
  1-minute linear slope estimate; wrong beta = losses beyond ₹1,500/trade.
- **Possible unclosed-candle use in live scans** (MEDIUM, unverified): no code
  excludes a still-forming 5-min bar from structure/breakout logic in the live
  scanner, though the backtest explicitly guards against exactly this class of
  look-ahead.
- **Backtest hardcodes lot_size=30** vs live dynamic contract master (LOW);
  single-stock-momentum gates 13:00 entries on a 09:45 relative-strength
  snapshot (LOW).

## 2. The `algobot/` platform: live-capable, gate not trustworthy

### 2.1 Live order path exists and is soft-gated only (CRITICAL)

- `algobot/broker/fyers/broker.py:51-76` — real `place_order` against FYERS.
- `algobot/broker/fyers/auth.py:186-233` — headless TOTP+PIN login, token
  cached in DB; no human step.
- `algobot/engine/scheduler.py:108,123-130` — live routing enables purely on
  successful FYERS auth; no `live_orders_enabled` fuse exists anywhere in
  `algobot/core/config.py` (contrast the fail-closed `SystemExit` validation
  in every `scripts/` loader). Kill switch defaults **off**.
- `codesfiles/s102_algobotstart.py:104,126,179-201` — standalone runnable live
  bot, places orders in a loop, includes **short CE/PE selling**, no gate.

### 2.2 Unauthenticated control plane, publicly bound (CRITICAL)

`algobot/api/main.py:56-61` (`allow_origins=["*"], allow_credentials=True`, no
auth dependency anywhere), `routes_control.py:139-155` (`promote` with
caller-supplied `force` → `lifecycle.py:132` skips the gate), and
`docker-compose.yml` publishes API `8000:8000` and dashboard `8501:8501` on all
interfaces with default `POSTGRES_PASSWORD=algobot` and a full read/write DB
role shared by engine, API, and dashboard (no least-privilege role, unlike
`dashboard_ro` in the legacy stack).

### 2.3 The paper→live gate would promote strategies whose track record was never real (CRITICAL/HIGH)

- Multi-leg option orders in paper/live bypass `RiskEngine` and margin sizing
  entirely (`execution/order_manager.py:100-126` uses hardcoded `leg.lots`,
  default 1); `estimate_margin` is used only by the backtest. Paper "capital
  used" is premium notional, not SPAN margin (`broker/paper.py:258-265`) — a
  short strangle consuming ₹2–5L real margin shows as consuming only the
  premium. The gate evaluates a risk profile that live trading will not have.
- The "stop-fire fidelity" gate metric is tautological: paper fills are
  `modeled ± configured slippage constant`, so the metric always equals the
  config constant and can never detect gap-through-stop risk
  (`broker/paper.py:150-165`, `gate.py:64-72`).
- `sample_ok = paper_trades OR oos_backtest_months` (`gate.py:116-122`): ~12
  months of synthetic-BS backtest (0.5 discount) substitutes for real paper
  trades, and PF/DD are then computed off however few paper trades exist.
- Backtest option pricing is internally inconsistent: strikes selected off a
  smile (`options/chain.py:21,82-83`) but priced/filled off flat trailing
  realized vol with no smile and no vol risk premium
  (`backtest/engine.py:550-575`, `backtest/option_data.py:146-163`) — biases
  premium-selling vs premium-buying comparisons, the platform's core decision.
- Short-premium strategies (op04 strangle is `intraday_squareoff=False`) can be
  held overnight through binary events with only a 15s polling soft-stop and no
  machine-enforced event calendar (`op10_long_vol_event.py:8` admits this).
- Global risk caps use one static `settings()["capital"]` (₹5L default) across
  all 49 strategies regardless of actual per-strategy `capital_alloc`
  (`engine/scheduler.py:94`, `execution/risk.py:27-77`).
- Costs: options STT modeled at 0.15% vs the current 0.10% sell-side rate
  (`costs/india.py:47-48`; the futures row was updated, options missed); flat
  0.20% slippage massively understates far-OTM wing fill costs; SPAN estimate
  is a static 13% of notional with no vol-regime scaling.
- Widespread `float` money math throughout `algobot/` (engine, portfolio,
  fills, paper broker, gate), violating the repo's own Decimal convention.

## 3. Correctness bugs (data pipeline & tests)

- **`ingest_fyers_history.py:168-177` and `ingest_fyers_optionchain.py:273-282`
  (HIGH):** on any failure, the error-status update is rolled back with the
  whole transaction (no `conn.commit()` before `raise`) — the run vanishes from
  `market.ingestion_runs` *and* all candles already upserted for earlier
  symbols in the batch are silently rolled back after stdout said they were
  stored. The identical bug was found and fixed in `ingest_fyers_quotes.py:191-194`
  with a regression test; the fix was never ported to the two siblings.
- **`ingest_fyers_quotes.py:167` (MEDIUM):** falls back to FYERS `chp`
  (change-*percent*) as a `close` price — can silently store `0.53` (a %) as a
  price in `market.quotes.close`.
- **`pyproject.toml` (HIGH, process):** the repo's default `pytest` run
  *ignores all 19 legacy safety-rail test files* via `addopts`, and CLAUDE.md's
  documented commands (`uv sync --group dev`) no longer work. The legacy tests
  still pass when run explicitly (162/163), but
  `test_dashboard_runner_refuses_external_bind_without_explicit_ack` **fails in
  any checkout** because `run_banknifty_options_dashboard.sh:3` hardcodes
  `cd /opt/data/finance-db` — the external-bind safety guard is untestable off
  the production box.
- `ingest_fyers_history.py`: no chunking/retry for year-long intraday ranges;
  `rows_updated` always 0. `compute_technical_factors.py`: one short-history
  symbol aborts the whole batch; `compute_for_symbols` is dead code.
  `test_technical_factors.py` (and `test_engine.py`/`test_execution.py`)
  require live Postgres with no skip guard, contradicting the stated
  no-DB-needed test convention.

## 4. Operations gaps for an unattended intraday system

- **No alerting of any kind** (HIGH): no slack/webhook/email/pager integration
  anywhere. If the tick engine crash-loops mid-day with an open position,
  stops silently stop being monitored; the watchdog only prints to stdout.
- `banknifty_options_eod_report.sh` and `banknifty_options_contract_refresh.sh`
  have no `flock` (and no DB autostart), contradicting CLAUDE.md.
- Tick wrappers gate on weekday/time but not `config/nse_holidays.yaml`.
- No log rotation; FYERS logs written indefinitely to `/tmp/`.
- Dashboard auto-refresh does a full page reload opening ~a dozen fresh
  Postgres connections per cycle (no pooling); `chain_ladder` hardcodes
  `"BANKNIFTY"` instead of reading the configured underlying.
- Postgres uses trust auth on loopback (documented, single-tenant assumption) —
  the SELECT-only guarantee rests entirely on OS-level isolation.

## 5. What held up well

- Legacy `scripts/` safety rails are genuinely solid: every config loader
  fail-closes on `paper_only`/`live_orders_enabled`, no order code exists under
  `scripts/`/`dashboard/`, the dashboard's `dashboard_ro` role +
  `conn.read_only=True` + `assert_readonly_sql` stack is correct, the control
  plane's column-scoped INSERT grant is properly least-privilege, cron tick
  wrappers use `flock -n` + `timeout` correctly, LLM spend is bounded and
  cached, and secrets hygiene is clean (nothing real committed).
- `banknifty_trend_patterns.py` and `option_chain_signals.py`: consistent
  Decimal usage, thorough zero/None guards, correct IST session handling.
- The team already found and honestly documented several look-ahead bugs in the
  backtests, and the proxy-backtest caveats are stated in the reports.

## 6. Prioritized actions

**Decide first (policy, not code):** is this repo paper-only or not? CLAUDE.md
and `algobot/` cannot both be true. Either (a) remove/quarantine
`algobot/broker/fyers/` and `codesfiles/s102_algobotstart.py`, or (b) update
CLAUDE.md and give `algobot/` the same fail-closed fuse the legacy system has
(a `live_orders_enabled` config check that hard-exits, kill switch default ON).

1. Lock the control plane: add auth to the FastAPI API, remove `force` from the
   network surface (or require a signed approval), bind API+dashboard to
   `127.0.0.1` in docker-compose, drop CORS `*`+credentials, set a real DB
   password and split least-privilege roles.
2. Delete or mark superseded the pre-bugfix backtest reports and re-tune exit
   parameters on fixed code; disable the flagship strategy until it passes its
   own gates or the gates are consciously revised.
3. Add real friction to paper P&L: fill at bid/ask (or LTP ± half-spread) and
   deduct the actual NSE cost stack in the live paper engine, so the number you
   watch converges toward what a live account would show.
4. Fix the two missing-commit ingestion bugs (port the quotes fix + its
   regression test), the `chp` fallback, and the failing/untestable dashboard
   bind-guard test (make the wrapper path-relative).
5. Add a dead-man's-switch alert (even a simple Telegram/email on tick-wrapper
   failure or stale-heartbeat) before running unattended again.
6. In `algobot/`: route multi-leg orders through RiskEngine + margin sizing,
   fix STT to 0.10%, make slippage moneyness-aware, replace realized-vol-as-IV
   or at least make strike selection and pricing use the same curve, and make
   the fidelity metric measure something falsifiable.
7. Restore `uv run pytest -q` as the single green gate: fix the dev-group /
   psycopg drift and stop ignoring the safety-rail tests by default.

**Bottom line on the money question:** nothing here is ready to risk capital.
The honest evidence is a 0.13R-expectancy flagship on 77 proxy trades and a
net-negative strategy pack, measured with a P&L model that omits theta, IV,
spreads, and fees. Treat current results as "worth more research," fix the
measurement layer first (costs + fills + real option data), and only then judge
whether any edge survives.
