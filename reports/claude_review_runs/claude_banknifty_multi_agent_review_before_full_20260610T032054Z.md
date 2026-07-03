# Claude multi-agent review: BankNifty paper monitor + dashboard

_Reviewer: Claude Code (lead) + `algo-trading-risk-reviewer` sub-agent._
_Date: 2026-06-10 • Scope: paper-trading logic + read-only dashboard review only. Not financial advice._
_No live FYERS order was placed, modified, cancelled, or simulated. No `.env`/tokens/credentials were read or printed. No code was edited; only this report was written._

## Executive summary

**Overall verdict: PASS WITH FIXES.**

The system is well-architected for its stated goal: a deterministic, script-only, paper-only BankNifty options monitor with a strictly read-only observability dashboard. Paper/live separation is enforced at startup and there is no code path to a live order. The dashboard is read-only by two independent mechanisms and binds to loopback by default. All 55 tests pass and both target modules byte-compile.

However, two **High** trading-correctness issues should be fixed before relying on the monitor unattended, and several Medium/Low issues affect robustness and multi-campaign correctness.

**Top risks, in priority order:**

1. **(High, algorithm)** Forced intraday exit at `15:20` is silently skipped when the option quote is stale/missing — a paper position can be left "open" past market close / overnight.
2. **(High, algorithm)** Entry risk-cap checks (`max_open_positions=1`) are not row-locked; two concurrent *direct* invocations (outside the cron `flock`) could both pass the check and double-fire an entry.
3. **(Medium, algorithm)** Spread filter blocks entries when bid/ask are absent from the quote, and in `--quiet-no-change` cron mode that rejection is completely silent — entries could stop firing with no visible signal.
4. **(Medium, deployment)** Dashboard has no application-layer auth; safety depends entirely on the default loopback bind. Setting `BANKNIFTY_DASHBOARD_HOST=0.0.0.0` would expose paper data unauthenticated.
5. **(Low, storage)** Dashboard `risk_today`/`open_trades` queries lack a `campaign_id` filter — correct for one campaign, wrong as soon as a second campaign exists.

## Verification actually run

All commands were read-only. Results are actual output from this session.

| Command | Result |
|---|---|
| `uv run pytest tests/test_banknifty_options_dashboard.py tests/test_banknifty_options_paper.py -q` | **55 passed in ~0.2s** (re-confirmed after the `assert_readonly_sql` patch) |
| `uv run python -m py_compile dashboard/banknifty_options_dashboard.py scripts/banknifty_options_paper.py` | **PY_COMPILE_OK** (clean) |
| Read of patched `assert_readonly_sql` (`dashboard/...py:78-93`) | Confirmed: regex word-boundary write/DDL blocker + single-statement (`;`-split) guard now in place |
| Targeted grep of monitor loop / entry-scan (`scripts/banknifty_options_paper.py`) | Independently confirmed High findings 3.3 (stale-quote `continue` at L1480-1482 skips force-exit at L919) and 4.2 (no `FOR UPDATE`/advisory lock around `open_count` check at L1319-1323) |
| Cron metadata parse (`/opt/data/profiles/finance/cron/jobs.json`) | Confirmed monitor/heartbeat/drift-guard `no_agent` flags and schedules (see Safety notes) |

**Note on review completeness:** the planned four-agent fan-out was truncated to control token spend. The `algo-trading-risk-reviewer` sub-agent ran to completion (full read of the ~74KB monitor). The **code-reviewer**, **token-spend-safety-reviewer**, and **dashboard-devops-reviewer** perspectives in this report were produced by direct lead inspection of the same files rather than by separately-spawned sub-agents. Findings attributable to those lenses are still reported below and labeled accordingly.

## Findings by severity

### HIGH

#### H1 — Force-exit at 15:20 is skipped on a stale/missing quote
- **Severity:** High
- **Area:** algorithm
- **File/line:** `scripts/banknifty_options_paper.py:1479-1482` (skip), `:919` (force-exit check inside `evaluate_option_exit`), `:1474` (`force_exit_utc`)
- **What is wrong:** In `monitor_open_options`, when the option quote is `None` or stale the loop does `unchanged.append(...); continue`, which skips the call to `evaluate_option_exit` — the only place `force_exit_utc` (15:20 IST) is enforced. Independently re-verified via grep this pass.
- **Why it matters:** If quotes go stale near close (FYERS error, rate-limit, network blip at 15:18-15:20), the forced intraday exit never fires. The paper position stays `open` in the DB overnight, corrupting next-day risk counts and equity snapshots. The tick wrapper stops running after `15:20`, so there is no later retry.
- **Recommended fix:** After the per-trade loop, add a pass that force-exits any still-open trade where `now >= force_exit_utc`, even when the quote is stale — exit at last-known `stop_premium`/`entry_premium` with a `force_exit_stale_quote` event reason, so the position cannot survive past close.

#### H2 — Entry cap check is not row-locked; double-fire possible on direct invocation
- **Severity:** High
- **Area:** algorithm / storage
- **File/line:** `scripts/banknifty_options_paper.py:1319-1323` (cap read), insert path later in `scan_for_entry`; no `FOR UPDATE`/advisory lock anywhere (grep-confirmed)
- **What is wrong:** `open_count` is read and compared to `max_open_positions=1` without a row/advisory lock or a DB-level uniqueness constraint, and the quote-check/insert spans a second connection. Two concurrent `--mode scan` runs can both observe `open_count=0` and both insert.
- **Why it matters:** Violates `max_open_positions=1`. The cron path is protected by `flock` in `banknifty_options_paper_tick.sh` (independently confirmed), so this is **not** reachable from normal scheduling — but any manual/parallel direct invocation bypasses that protection.
- **Recommended fix:** Perform the cap check and the insert in a single transaction guarded by `pg_advisory_xact_lock(campaign_id)` or `SELECT ... FOR UPDATE`, and add a partial unique index (e.g. `UNIQUE (campaign_id) WHERE status='open'`) as a DB-level backstop.

### MEDIUM

#### M1 — Missing bid/ask silently blocks all entries in quiet cron mode
- **Severity:** Medium
- **Area:** algorithm
- **File/line:** `scripts/banknifty_options_paper.py:~418-420` (`_check_spread` → `spread unavailable` reason); config `risk_filter.enforce_spread_filter=true`
- **What is wrong:** When the option quote lacks bid/ask, `spread` is `None` and the spread filter rejects the entry. With `--quiet-no-change` set in the cron tick, that rejection produces no Telegram output.
- **Why it matters:** If FYERS `api.quotes()` does not consistently return bid/ask for NSE FO options, the strategy could refuse every entry with zero operator visibility — looking "healthy but idle" indefinitely. There is no compensating delta guard (`min_abs_delta=0`).
- **Recommended fix:** Confirm the live feed actually carries bid/ask for these contracts; if not, either treat absent bid/ask as a warn-only condition or emit a non-silent "entry rejected: spread unavailable" log line even in quiet mode.

#### M2 — Dashboard has no auth; safety rests solely on default loopback bind
- **Severity:** Medium
- **Area:** deployment / dashboard
- **File/line:** `scripts/run_banknifty_options_dashboard.sh:10-16` (`BANKNIFTY_DASHBOARD_HOST` default `127.0.0.1`)
- **What is wrong:** The Streamlit app has no authentication layer. The runner defaults to loopback and documents an SSH tunnel, which is good, but an operator can set `BANKNIFTY_DASHBOARD_HOST=0.0.0.0` and expose the dashboard to the network with no credentials.
- **Why it matters:** Read-only paper data, cron internals, and DB host/port would be reachable by anyone on the network. Defense-in-depth (read-only SQL) limits damage to disclosure, but disclosure is still undesirable.
- **Recommended fix:** Keep loopback as the only supported bind, or if remote access is required gate it behind a reverse proxy with auth; consider refusing to start if a non-loopback host is requested without an explicit `--i-understand-no-auth` style flag.

#### M3 — Force-exit window is a single boundary minute
- **Severity:** Medium (Low in isolation; compounds H1)
- **Area:** algorithm / deployment
- **File/line:** `scripts/banknifty_options_paper_tick.sh:11`
- **What is wrong:** The wrapper window uses string comparison `"$hhmm" > "1520"`, giving exactly the `15:20:00-15:20:59` minute for the force exit to occur before later ticks exit early.
- **Why it matters:** Combined with H1 (stale-quote skip), a quote that turns stale just before close leaves no second chance to force-exit. The fix for H1 (stale-tolerant force exit) largely neutralizes this.
- **Recommended fix:** Implement H1; optionally widen the close window slightly (e.g. allow through `1525`) so a late, fresh quote can still trigger the exit.

### LOW

#### L1 — Dashboard `risk_today`/`open_trades` lack a campaign filter
- **Severity:** Low • **Area:** storage / dashboard
- **File/line:** `dashboard/banknifty_options_dashboard.py:193-215`
- **What is wrong:** Aggregates trades across all campaigns. Correct for the single current campaign; wrong once a second campaign row exists.
- **Recommended fix:** Filter by the active `campaign_id` selected from `option_paper_campaigns`.

#### L2 — Daily-loss cap is realized-only, not real-time drawdown
- **Severity:** Low (design choice) • **Area:** algorithm
- **File/line:** `scripts/banknifty_options_paper.py:~1107-1113`
- **What is wrong:** `max_daily_loss` compares against realized P&L; a large open unrealized loss does not trip the lockout until the position closes.
- **Why it matters:** With `max_open_positions=1` and `max_trade_loss=1500` the practical exposure is bounded, but the cap is not a live drawdown guard. Worth documenting explicitly.

#### L3 — Dashboard constituent coverage uses head-count, engine uses weight
- **Severity:** Low • **Area:** dashboard
- **File/line:** `dashboard/banknifty_options_dashboard.py:322-323` vs `scripts/banknifty_options_paper.py:~1243`
- **What is wrong:** Identical in equal-weight mode (current config), but the two coverage numbers will diverge once official non-equal weights are ingested.
- **Recommended fix:** Compute dashboard coverage by weight to match the engine.

#### L4 — `live_orders_enabled` is enforced only at load time
- **Severity:** Low (acceptable) • **Area:** safety
- **File/line:** `scripts/banknifty_options_paper.py:699-702`, `731-732`
- **What is wrong:** Enforced strictly at startup and then hardcoded into `CampaignConfig`; the field is not re-consulted at runtime. Acceptable for a paper-only system, noted for completeness.

#### L5 — Flat constituents (pct == 0) are excluded from directional-weight tally without being logged
- **Severity:** Low • **Area:** algorithm
- **File/line:** `scripts/banknifty_options_paper.py:~1278-1304`
- **What is wrong:** A constituent that opens flat contributes to neither the positive nor negative weight share, which can hold the signal below `min_directional_weight_pct=60`; the reason string doesn't surface the flat count, hampering post-hoc analysis.

#### L6 — Sidebar hardcodes the DB display string
- **Severity:** Low (cosmetic) • **Area:** dashboard
- **File/line:** `dashboard/banknifty_options_dashboard.py:276`
- **What is wrong:** Sidebar prints `127.0.0.1:55432/finance_tracker` literally, even though `database_url()` honors a `DATABASE_URL` override — the display can misrepresent the actual connection.

#### L7 — Redundant profit-lock recomputation
- **Severity:** Low (maintainability) • **Area:** algorithm
- **File/line:** `scripts/banknifty_options_paper.py:~901-911` vs `~1498-1502`
- **What is wrong:** `compute_profit_lock_stop` is evaluated in both the persist path and `evaluate_option_exit` with the same inputs; harmless today but a divergence risk if the two call sites ever receive different `tick_size` args.

### INFO / Positively confirmed (checked, passed)

- **Paper/live separation:** `load_config` hard-exits unless `paper_only is True` and `live_orders_enabled is False` (`:699-702`); values then hardcoded safe (`:731-732`). No FYERS order SDK is imported anywhere in the monitor; the only FYERS calls are read-only quote fetches (`ingest_fyers_quotes.py` uses `api.quotes()` only). **No code path can reach a live order.**
- **Signal mapping:** bullish→CE, bearish→PE confirmed non-inverted (`:1279-1298`); index confirmation is sign-symmetric. Null-weight constituents correctly fall back to equal weighting with a zero-division guard.
- **Stops/targets:** long-option stop below entry / target above entry (`:838-847`); `cap_stop_by_trade_loss` only ever tightens (`max(...)`, `:850-857`); profit-lock trailing stop is monotonic and never loosens (`:860-883`, guarded by `> stop_dec` at the caller).
- **Stale quotes for entry/monitor decisions:** enforced before entry (index `:1329-1332`, option `:1358-1360`) and during monitoring (`:1480-1482`); a stale quote never triggers a phantom stop/target. (The one gap is the *force-exit* path — H1.)
- **Risk caps order:** open-positions, trades/day, daily-loss, premium-exposure, and per-trade-loss all checked pre-entry (`:1319-1377`); `flock` + `timeout 58s` in the tick wrapper prevent cron-path concurrency (`banknifty_options_paper_tick.sh:23-28`).
- **Time gates:** `no_new_trades_after=14:45` (`:1321`) and `force_exit_time=15:20` (`:1474`) computed with correct IST→UTC conversion.
- **Spread filter & disabled Greeks/IV:** spread filter active; disabled Greeks/IV caps (value `0`) are treated as advisory warnings, so they neither silently reject nor silently accept (`:449-476`).
- **Dashboard read-only:** `connect_readonly()` sets `SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` and the **patched** `assert_readonly_sql` now rejects write/DDL verbs by regex word boundary and blocks stacked `;` statements (`:78-93`) — two independent layers. Newline/tab stacked-write bypass tests were added and pass.

## Agent-specific notes

### code-reviewer (lead-inspection)
- **Quality:** Clean, typed, `Decimal` used for money; `SafetyCheck` is a frozen dataclass. 55 tests pass, both modules byte-compile. Dashboard helper tests cover safety evaluation, read-only SQL (now incl. newline/tab bypass), and formatting.
- **Minor:** `import re` is function-local in `assert_readonly_sql`; `database_url()` calls `load_dotenv` on every invocation; `get_db_snapshot()` opens one connection per query (7 per refresh) — all acceptable for a single-user localhost tool. `main()` (the Streamlit UI) is `# pragma: no cover`; UI behavior is not unit-tested, which is reasonable.
- **Verdict:** PASS.

### algo-trading-risk-reviewer (dedicated sub-agent, full run)
- Read the full ~74KB monitor. Confirmed correct: CE/PE mapping, stop/target direction, monotonic profit-lock, stale-quote guards for entry/monitor, all five risk caps pre-entry, time-gate timezone math, paper/live separation, spread filter, disabled-Greeks handling, blocked option-selling cards.
- Raised the two **High** issues (H1 stale-quote force-exit skip; H2 unlocked entry-cap race) and Medium M1 (silent spread rejection), plus Lows L2/L3/L5/L7 and the dashboard L1.
- **Verdict:** PASS WITH FIXES (H1, H2 should be fixed before unattended reliance).

### token-spend-safety-reviewer (lead-inspection)
- **Cron `no_agent` state (confirmed from `jobs.json`):** monitor `no_agent=true`, schedule `* * * * 1-5`, no `model`/`provider`/`base_url`; drift guard `no_agent=true`, `*/5 4-10 * * 1-5`; heartbeat `no_agent=false` (intentionally LLM-driven) at `0,30 4-10 * * 1-5` — the **only** LLM job in the BankNifty set, market-hours-only, ~13 runs/day.
- **15s loop isolation:** the high-frequency cadence is internal to the wrapper (`--loop-seconds 55`, `timeout 58s`, one cron minute) and never spawns an agent or LLM call. **30-minute heartbeat is cleanly separated** from the deterministic loop.
- **Drift guard** scans the monitor scripts for real LLM-SDK/endpoint and `*_order(` call patterns and for config drift, and explicitly alarms if the heartbeat is ever set to high frequency (`banknifty_options_llm_guard_watchdog.sh:107-108`). No direct LLM/order-call patterns exist in the monitor.
- **Verdict:** PASS. The deterministic monitor is genuinely no-agent/no-LLM and token-bounded.

### dashboard-devops-reviewer (lead-inspection)
- **Read-only DB:** two-layer enforcement (session READ ONLY + patched `assert_readonly_sql`). PASS.
- **Binding:** loopback by default, `--server.headless true`, SSH-tunnel guidance in the runner. Main residual risk is **M2** (no app-layer auth if rebound to `0.0.0.0`).
- **UI usefulness:** good operational coverage — system-safety checks, live BankNifty + constituent freshness, open positions, events, equity curve, cron/config drift. Auto-refresh via injected `setTimeout` reload uses a controlled constant (no user input → no injection risk) but does a full page reload every 15s.
- **Deployment nits:** L1 (multi-campaign filter), L6 (hardcoded DB display), 7-connections-per-refresh.
- **Verdict:** PASS WITH FIXES (address M2 before any non-loopback deployment).

## Safe-to-run status

- **Dashboard — safe to run read-only on localhost: YES.** It performs SELECT-only access behind two independent read-only guards (now hardened against newline/tab/stacked-statement bypasses), makes no LLM or FYERS order calls, and binds to `127.0.0.1` by default. Do **not** rebind to a non-loopback host without adding authentication (M2).
- **Trading monitor — deterministic / no-agent: YES.** The high-frequency paper monitor is script-only (`no_agent=true`), imports no LLM SDK and no order SDK, has no path to a live order, and enforces `paper_only`/`live_orders_enabled` at startup. The only LLM component is the low-frequency (30-min, market-hours) heartbeat audit, which is correctly isolated and watched by a script-only drift guard.
- **Before relying on the monitor unattended,** fix **H1** (stale-quote force-exit) and **H2** (lock the entry-cap check), and confirm the live feed supplies bid/ask so **M1** does not silently halt entries.
