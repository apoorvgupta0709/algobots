# Claude multi-agent review: BankNifty paper monitor + dashboard

_Review date: 2026-06-10 • Scope: `dashboard/banknifty_options_dashboard.py`, `scripts/banknifty_options_paper.py`, the tick/heartbeat/guard wrappers, `config/banknifty_options_paper.json`, and the finance-profile cron metadata. Read-only pass: no code edited, no live order API touched, no secrets read._

## Executive summary

- **Overall verdict: PASS WITH FIXES**
- The core safety invariants all hold and were affirmatively verified:
  - **No live-order code path exists anywhere.** `grep` for `place_order/modify_order/cancel_order/exit_order/create_order` across the monitor, ingest, and tick scripts returns zero matches. The only FYERS call is read-only `api.quotes()`.
  - **Hard paper-only gates.** `load_config()` raises `SystemExit` if `paper_only != true` or `live_orders_enabled == true`, and then hardcodes both safe values regardless of JSON (`banknifty_options_paper.py:699-702,731-732`).
  - **The high-frequency monitor is deterministic and script-only.** Cron job `32e425a2c14b` is `no_agent=true`, `model/provider/base_url=null`. No LLM imports or endpoints anywhere in the hot loop. The 15s polling is pure Python+DB. `flock -n` + `timeout 58s` + `--loop-seconds 55` prevent overlap/stacking inside the 1-minute cron.
  - **Bounded LLM spend.** Only the 30-min heartbeat (`0,30 4-10 * * 1-5`, `no_agent=false`) spends tokens — worst case ≈14 invocations/weekday (≈14–42 LLM turns with tool rounds). No fan-out or recursion. The drift guard (`*/5`, `no_agent=true`) and monitor add zero LLM cost.
  - **The dashboard is read-only in practice.** No FYERS calls, no disk writes, all SQL hardcoded and parameterized, app-layer `assert_readonly_sql()` gate, graceful degradation on failure.

- **Top risks, in priority order:**
  1. **(Critical, algorithm)** Stale/missing quote during monitoring skips exit evaluation entirely, including the force-exit at 15:20 IST — a paper trade can outlive the day and corrupt P&L/risk accounting (`banknifty_options_paper.py:1480-1482`).
  2. **(High, algorithm/config)** `max_trades_per_day (4) × max_trade_loss (₹1500) = ₹6000 > max_daily_loss (₹5000)`, and the daily-loss lockout only counts **realized** P&L — the daily cap can be breached by ~20%.
  3. **(Critical defect, contained impact, storage)** The dashboard's Postgres-layer read-only guarantee is silently inactive: `transaction_read_only=off` on every query and the DB user is a **superuser**. Verified live. Impact is contained today only because all SQL is hardcoded with no user-input path.
  4. **(High, deployment)** `streamlit` is unpinned (installed via `uv run --with streamlit`, absent from `pyproject.toml`/`uv.lock`) and `BANKNIFTY_DASHBOARD_HOST` can be set to `0.0.0.0` exposing an auth-less dashboard.
  5. **(Medium, safety hygiene)** Several small drift-guard coverage gaps and a few latent correctness edges (NaN/Inf formatting crashes, tick-size inconsistency, TOCTOU on entry).

## Verification actually run

| Command | Result |
|---|---|
| `uv run python -m py_compile dashboard/banknifty_options_dashboard.py scripts/banknifty_options_paper.py` | **PASS** (`PY_COMPILE_OK`) |
| `uv run pytest tests/test_banknifty_options_dashboard.py tests/test_banknifty_options_paper.py -q` | **PASS — 55 passed in 0.21s** |
| Read-only smoke: import dashboard, `evaluate_system_safety(real config, real jobs)` | **All 11 safety checks pass** against the live config/jobs |
| Read-only smoke: `get_db_snapshot()` | Executes; DB reachable; returns live rows |
| Live verification of the read-only-transaction claim (reproduced the `fetch_rows` flow) | `transaction_read_only` = **`off`** during query; `default_transaction_read_only` = `on`; `current_user` **`rolsuper=True`** |
| Code read of `monitor_open_options` stale-quote branch (`:1474-1516`) | Confirmed: `if ltp is None or stale: ... continue` skips `evaluate_option_exit()` (the only force-exit gate) |
| `grep` for live-order / LLM call patterns across monitor + ingest + tick scripts | Zero real calls; matches in heartbeat/guard scripts are detection regexes only |

> Note: a prior run's artifacts already existed in `reports/` (`..._stream.jsonl`, `..._resume.json`, an earlier markdown). This report is a fresh, independent multi-agent pass and overwrites the markdown deliverable.

## Findings by severity

### CRITICAL

**C1 — Stale/missing quote during monitoring skips exit evaluation, including force-exit**
- Area: algorithm
- File: `scripts/banknifty_options_paper.py:1480-1482` (inside `monitor_open_options`)
- What is wrong: When a quote is missing or older than `quote_stale_seconds` (90s), the loop does `unchanged.append(...); continue`, bypassing `evaluate_option_exit()` — which is the **only** place `force_exit_utc` (15:20 IST), stop, and target are checked. The 15s poll keeps logging "cannot evaluate exit" with no escalation.
- Why it matters: If the feed freezes at/after force-exit time, the paper trade is never closed; it stays `open` indefinitely. That (a) corrupts daily P&L/equity accounting (the snapshot's unrealized P&L join doesn't filter staleness either — see M5), and (b) keeps `open_positions`/`trades_today` caps occupied. Paper-only, so no real money, but it breaks the deterministic accounting the whole system relies on.
- Recommended fix: Add a time-based fallback — when `now >= force_exit_utc` and the quote is stale, close the paper trade at last-known `entry_premium` (or last good premium), marked e.g. `force_exit_stale`, rather than `continue`.

**C2 — Dashboard Postgres read-only transaction is not actually enforced (and DB user is superuser)**
- Area: storage / dashboard safety
- File: `dashboard/banknifty_options_dashboard.py:71-75` (`connect_readonly`)
- What is wrong: `conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")` runs *inside* the first implicit transaction (psycopg3 default `autocommit=False`). `SET SESSION CHARACTERISTICS` only affects transactions started **after** the current one, so the actual query transaction in `fetch_rows` runs READ WRITE. **Verified live:** `SHOW transaction_read_only` returns `off` during the query while `default_transaction_read_only` is `on`. Compounding it, the `hermes` DB role is a **superuser** (`rolsuper=true`), which can override read-only anyway.
- Why it matters: The code comment promises "if a future dashboard query accidentally writes, Postgres refuses it" — that backstop is silently absent. Today the only thing preventing writes is the app-layer `assert_readonly_sql()` + the fact that all SQL is hardcoded with no user-input path, so real-world risk is low. But any future debug-SQL widget or careless extension would write with no DB-level guard.
- Recommended fix (both): (1) Make read-only real on the connection — e.g. set `conn.autocommit=True`, run the `SET`, then `conn.autocommit=False`; or pass `options="-c default_transaction_read_only=on"` in the DSN; or use `conn.read_only=True` before any execute. (2) Create a least-privilege `dashboard_ro` role with `SELECT`-only grants on `research`/`market` and point `DATABASE_URL` at it. (2) is the durable fix and also closes M3 below.

### HIGH

**H1 — Daily-loss cap is structurally breachable; lockout ignores unrealized P&L**
- Area: algorithm / config
- File: `scripts/banknifty_options_paper.py:1327` (lockout check) + `config/banknifty_options_paper.json:8-11`
- What is wrong: The lockout at entry checks only `realized_today <= -max_daily_loss`. With `max_trades_per_day=4` and `max_trade_loss=₹1500`, three realized losses (-₹4500) are still `> -₹5000`, so the 4th trade fires and can realize a 4th -₹1500, totaling **-₹6000 (20% over the ₹5000 cap)**. An open trade's unrealized loss never contributes to the halt.
- Why it matters: The advertised ₹5000 daily-loss guardrail does not actually bound the day's loss given the other caps.
- Recommended fix: Enforce `max_trades_per_day × max_trade_loss ≤ max_daily_loss` (e.g. drop `max_trades_per_day` to 3, or `max_trade_loss` to ₹1250), and/or include open unrealized P&L in the lockout check.

**H2 — `streamlit` is an unpinned, runtime-installed dependency**
- Area: deployment / supply-chain
- File: `scripts/run_banknifty_options_dashboard.sh:13`; absent from `pyproject.toml` and `uv.lock` (confirmed zero occurrences)
- What is wrong: `uv run --with streamlit ...` resolves Streamlit from PyPI at run time with no lockfile pin. Non-reproducible builds; a breaking Streamlit release or a compromised release is absorbed silently on next run.
- Recommended fix: Add `streamlit` to a dependency group in `pyproject.toml` (e.g. `streamlit>=1.35,<2`), regenerate `uv.lock`, and change the runner to `uv run streamlit run ...`.

**H3 — `BANKNIFTY_DASHBOARD_HOST` can expose an auth-less dashboard on `0.0.0.0`**
- Area: deployment / dashboard safety
- File: `scripts/run_banknifty_options_dashboard.sh:10`
- What is wrong: Default `127.0.0.1` is correct, but overriding the env var to `0.0.0.0` binds all interfaces with no auth (Streamlit has none). The dashboard then serves project path, DB host:port/dbname, live P&L, risk params, and trade events publicly. The SSH-tunnel pattern is documented in a comment but not enforced.
- Recommended fix: Guard in the runner — abort if `HOST != 127.0.0.1` unless an explicit `BANKNIFTY_DASHBOARD_ALLOW_EXTERNAL=yes` is set; require a reverse proxy with auth for any non-loopback bind. Document in README.

**H4 — Quote-refresh failure aborts the whole monitor cycle (no exit evaluation)**
- Area: algorithm / robustness
- File: `scripts/banknifty_options_paper.py:946-953` (`refresh_quotes`, `subprocess.run(..., check=True)`); call sites `:1315,1354,1471`
- What is wrong: A non-zero exit from `ingest_fyers_quotes.py` (FYERS timeout, expired token, transient network) raises `CalledProcessError` uncaught, killing `scan_for_entry`/`monitor_open_options` for that 60s window. Combined with C1, this widens the window where an open trade is never evaluated for stop/target/force-exit.
- Recommended fix: Wrap the refresh call sites in `try/except`, log a warning, and proceed with existing DB quotes (the staleness gate then governs whether to act — and should be paired with the C1 force-exit fallback).

> The algo reviewer also flagged a "double profit-lock computation" (`:1490-1515` then again inside `evaluate_option_exit:902/910`) as High-redundancy. On review it is **functionally correct** — the second call is idempotent and only ever raises the stop further, never loosens it. Treated as Low/Info cleanup (L9), not a correctness bug.

### MEDIUM

**M1 — Dashboard `inr()` / `pct_from_open()` crash on NaN/Infinity**
- Area: dashboard / tests
- File: `dashboard/banknifty_options_dashboard.py:151,164`
- What is wrong: `inr(Decimal('NaN'))` raises `InvalidOperation` at the `amount < 0` comparison; `pct_from_open('inf',100)` raises on `.quantize()`. The `except` guards wrap only the `Decimal(str(...))` conversion, not the later arithmetic. SQL-computed `unrealized_pnl` can in rare cases surface a non-finite numeric and crash the render.
- Recommended fix: After conversion, `if not amount.is_finite(): return str(value)` (and the analogous guard in `pct_from_open`). Add NaN/Inf tests.

**M2 — Tick-size inconsistency between entry and monitor stops**
- Area: algorithm
- File: `scripts/banknifty_options_paper.py:1376-1377` (entry uses `contract.tick_size`) vs `:1496,1515` (monitor uses `config.option_tick_size`)
- What is wrong: Entry stop/target round to the contract's tick; the trailing stop and exit rounding use the global config tick (`0.05`). Equal today, but a contract whose actual tick differs would produce inconsistent rounding between initial and trailing stops.
- Recommended fix: Persist `contract.tick_size` on the trade row and use it consistently in monitoring.

**M3 — `assert_readonly_sql` allows superuser file-read functions inside SELECT**
- Area: dashboard / storage
- File: `dashboard/banknifty_options_dashboard.py:78-93`
- What is wrong: `SELECT pg_read_file(...)`, `pg_ls_dir(...)`, `lo_get(...)`, `pg_read_binary_file(...)` pass the guard and, because the role is superuser, would actually read server files. No user-SQL path today, so latent.
- Recommended fix: Add these function names to the banned regex, and (durably) the `dashboard_ro` role from C2 removes the privilege entirely.

**M4 — Entry checks vs INSERT are in separate transactions (TOCTOU)**
- Area: algorithm / storage
- File: `scripts/banknifty_options_paper.py:1316-1351` (cap checks) vs `:1355-1448` (insert), with `refresh_quotes` in between
- What is wrong: `max_open_positions`/`max_trades_per_day`/`max_daily_loss` are checked in one transaction; the INSERT happens in a later one. Concurrent runs could both pass and both insert. Protected in production only by the shell `flock` — not by the Python, so a direct `--mode scan` invocation outside the wrapper is unguarded.
- Recommended fix: Re-check the open-position/count guard inside the insert transaction (`SELECT ... FOR UPDATE` or `pg_advisory_xact_lock(campaign_id)`).

**M5 — Snapshot/dashboard unrealized P&L and BankNifty quote don't filter stale rows**
- Area: storage / algorithm
- File: `scripts/banknifty_options_paper.py:1546` (snapshot join, no staleness filter); `dashboard/banknifty_options_dashboard.py:193-199` (`banknifty_quote` has no `ORDER BY/LIMIT 1`); `:347-350` (equity chart dict silently drops duplicate `snapshot_date` — confirmed two rows for 2026-06-08)
- What is wrong: Stale quotes inflate/mislead unrealized equity; a duplicate quote row could be picked arbitrarily; duplicate daily-snapshot dates drop a data point from the chart while the table shows both.
- Recommended fix: Add `ORDER BY updated_at DESC LIMIT 1` to the quote query; de-dup snapshot dates (`keep='last'`) for the chart; consider a staleness filter on the unrealized-P&L join.

**M6 — Drift-guard coverage gaps**
- Area: safety
- File: `/opt/data/profiles/finance/scripts/banknifty_options_llm_guard_watchdog.sh:29-43,107`
- What is wrong: (a) The guard does not scan **itself** (`SCRIPT_PATHS` excludes the watchdog) — a mutation adding an LLM call to the guard would go undetected until the 30-min heartbeat. (b) The `agent_cli` regex matches shell forms (`hermes chat`) but not a Python `subprocess.run(["hermes","chat",...])`. (c) The explicit high-frequency check at `:107` is redundant given the exact-match at `:104`, creating a false sense of dedicated coverage.
- Recommended fix: Add the guard script to `SCRIPT_PATHS`; add a `subprocess.*\b(hermes|claude|codex)\b` pattern; collapse the redundant `:107` check or comment that `:104` is the enforcer.

### LOW

- **L1** `dashboard:71-101` — `connect_readonly()` semantics are confusing (returns an already-begun transaction; works coincidentally). Clean up alongside C2.
- **L2** `dashboard:175` `age_status` returns `OK` for **negative** age (clock skew) — should be `WARN`/`UNKNOWN`.
- **L3** `dashboard:78-93` `assert_readonly_sql` doesn't strip NUL bytes or block `MERGE`/`REFRESH MATERIALIZED VIEW`; verified `"select\x00 1"` passes the guard. Defense-in-depth only.
- **L4** `dashboard:28-31` dotenv fallback catches `except Exception` instead of `ImportError`.
- **L5** `dashboard:37-38` `DEFAULT_DB_PORT = str(55_000 + 432)` and string-concatenated DSN hurt grep-ability; the literal `55432` lives in 4+ places with no single source of truth.
- **L6** `banknifty_options_paper.py:215-217` `parse_time` crashes on `"14:45:00"` (uses `split(":",1)` then `int("45:00")`); brittle config-time parsing.
- **L7** `banknifty_options_paper.py:704` fallback default `campaign_name` encodes wrong capital (`..._5000_...` vs `starting_capital=50000`).
- **L8** `banknifty_options_paper.py:1347` HTTP fetch held inside an open DB transaction (up to 60s).
- **L9** `banknifty_options_paper.py:1490-1516` redundant double profit-lock computation — correct but worth a clarifying comment or `highest_premium=None` on the second call.
- **L10** `banknifty_options_paper_tick.sh:11` market-hours gate uses lexicographic `<`/`>` on `HHMM` — correct for zero-padded `%H%M`, fragile if format changes; prefer `(( 10#$hhmm ... ))`.
- **L11** Dashboard opens 7 connections/refresh/tab, no pooling, full-page JS reload (`components.html` `setTimeout` can stack across reruns). Functional but wasteful; prefer `streamlit-autorefresh` + a pooled connection.
- **L12** `dashboard:215-222` `risk_today` query applies per-row timezone conversion, defeating any `created_at` index — fine at 3 rows, scales poorly.

### INFO / affirmative confirmations

- **No live order path** anywhere (monitor, ingest, tick): zero `*_order(` matches; only read-only `api.quotes()`.
- **Hard paper-only enforcement** with `SystemExit` + hardcoded safe values (`:699-702,731-732`).
- **Option-selling double-blocked** via `enabled=false`/`paper_trade_enabled=false` flags *and* `entry_function` blocklist + `card_type=='entry'` gate (`:629-643`, config router).
- **Signal mapping correct**: net-bullish→CE, net-bearish→PE, sign verified (`:1278-1298`); equal-weight 1/14 fallback correct when all weights null (`:1172-1181`); all three gates (coverage 70% / directional 60% / index 0.05%) enforced pre-entry.
- **Stops/target/profit-lock correct**: long-option stop below / target above entry (`:838-847`); `max_trade_loss` and `stop_loss_pct` resolve as independent floors, tighter wins (`:850-857`); trailing stop is strictly monotone-up and can never go below entry (`:881,1498,910`).
- **Entry caps enforced** (`no_new_trades_after`, `max_open_positions`, `max_trades_per_day`, `max_daily_loss`) at `:1321-1328`; `force_exit_time` correctly converted IST→UTC (`:1474`).
- **Determinism**: no `random`/`uuid`/wall-clock nondeterminism/LLM/network in the hot loop; `flock` + `timeout 58s` + `--loop-seconds 55` prevent overlap/stacking.
- **Dashboard**: no FYERS calls, no disk writes, parameterized SQL (jsonb via `%s::jsonb`), graceful `try/except → st.exception`, no credential exposure (sidebar prints only host:port/dbname, no password; config/cron tabs omit secrets).
- **`evaluate_system_safety()`** passes all 11 checks against the real config/jobs; the safety panel fails **closed** (a renamed job → red banner).

## Agent-specific notes

**code-reviewer** — 24 findings. Headline: the read-only transaction is not enforced (C2 here) and `inr()`/`pct_from_open()` NaN/Inf crashes (M1). Real correctness edges: `refresh_quotes` failure propagation (H4), `parse_time` seconds crash (L6), HTTP-in-transaction (L8), TOCTOU (M4). Strong test-coverage gaps: `evaluate_constituent_led_direction`, `get_db_snapshot`, the pure config parsers, and the dashboard NaN paths are untested despite being trivially testable. Maintainability nits around duplicated `55432`/DSN/job-name constants. Baseline confirmed green (55 tests, py_compile).

**algo-trading-risk-reviewer** — Methodical, affirmatively confirmed every safe control with line refs (signal mapping, gates, stops, profit-lock monotonicity, caps, paper/live separation, determinism). One genuine Critical (C1 stale-quote force-exit skip), one genuine High (H1 daily-loss breach math), plus tick-size (M2), TOCTOU (M4), and stale-quote accounting (M5). Verified the "double profit-lock" is redundant-but-correct, not a bug. Conclusion: the engine is paper-safe with no live-order surface; the gaps are accounting/edge-case integrity, not real-money risk.

**token-spend-safety-reviewer** — All 14 affirmed controls hold: monitor `no_agent=true` with null model/provider, no LLM in the hot path, heartbeat is the only intentional (low-frequency) LLM spend, profile wrapper is a bare `exec`, `flock`+`timeout` bound runtime. Max LLM bound ≈14 heartbeat invocations/weekday, no fan-out. Findings are all Low/Info: HHMM comparison (L10), three drift-guard coverage gaps (M6), heartbeat `file` toolset is write-capable (instruction-restricted only), and postgres-down exits cleanly with no agent escalation. **No High/Critical.**

**dashboard-devops-reviewer** — Smoke-imported the module, ran `evaluate_system_safety` (11/11) and `get_db_snapshot()` (DB reachable), and **verified the read-only-transaction defect live** (`transaction_read_only=off`, superuser role). Findings: C2 (read-only not enforced), H2 (unpinned streamlit), H3 (0.0.0.0 exposure), plus Mediums (superuser file-read functions, missing `LIMIT 1` on quote, equity-chart de-dup, hardcoded profile path). Verdict: **safe to run read-only on localhost**, with the Postgres-layer guarantee to be made real before extending. Recommends a dedicated `dashboard_ro` role as the single highest-value fix.

## Safe-to-run status

- **Dashboard — SAFE to run read-only on localhost (127.0.0.1).** It makes no FYERS/LLM calls, writes nothing to disk, uses only hardcoded parameterized SELECTs gated by `assert_readonly_sql()`, and degrades gracefully. The DB-layer read-only promise is currently inert (C2) and the DB role is a superuser, but with no user-SQL path this is a defense-in-depth gap, not an active write risk. **Do not** bind to `0.0.0.0` without an authenticating reverse proxy (H3), and pin `streamlit` before relying on it (H2). Recommended highest-value hardening: create a least-privilege `dashboard_ro` role (fixes C2 + M3 at the DB layer).
- **Trading monitor — remains deterministic and no-agent.** Confirmed: `no_agent=true`, null model/provider/base_url, no LLM/agent in the 15s loop or any code path, no live-order API anywhere, hard `SystemExit` paper-only gates, bounded token spend isolated to the separate 30-min heartbeat, and `flock`/`timeout` overlap protection. The two engine issues to fix (C1 stale-quote force-exit fallback, H1 daily-loss cap math) are paper-accounting/risk-bound correctness gaps — they do **not** introduce live-order or token-spend risk, and the determinism/no-agent property is intact.

_This is a paper-trading logic and safety review only — not financial advice. No live FYERS order was placed, modified, cancelled, or simulated; no secrets were read._
