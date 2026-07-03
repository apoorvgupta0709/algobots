You are Claude Code running inside `/opt/data/finance-db` for Apoorv's finance profile.

Task: perform a thorough multi-agent review of the BankNifty options paper-monitoring system and the new read-only Streamlit dashboard.

Context and files to inspect:
- Dashboard implementation: `dashboard/banknifty_options_dashboard.py`
- Dashboard runner: `scripts/run_banknifty_options_dashboard.sh`
- Dashboard tests: `tests/test_banknifty_options_dashboard.py`
- Paper monitor: `scripts/banknifty_options_paper.py`
- Monitor wrapper: `scripts/banknifty_options_paper_tick.sh`
- Config: `config/banknifty_options_paper.json`
- Finance-profile cron metadata: `/opt/data/profiles/finance/cron/jobs.json`
- Finance-profile wrappers: `/opt/data/profiles/finance/scripts/banknifty_options_paper_tick.sh`, `/opt/data/profiles/finance/scripts/banknifty_options_heartbeat_audit.sh`, `/opt/data/profiles/finance/scripts/banknifty_options_llm_guard_watchdog.sh`

Strict safety rules:
- Do not place, modify, cancel, or simulate any live FYERS order.
- Do not call any live order API.
- Do not read or print `.env`, auth tokens, API keys, FYERS credentials, or account identifiers.
- Do not edit code in this review pass. Only write the final review report.
- It is OK to run read-only tests and read-only SQL via existing helper scripts.
- Treat all trading conclusions as paper-trading logic review only, not financial advice.

Use/spawn these specialized review agents and combine their findings:
1. `@code-reviewer` — Python code quality, correctness, test coverage, runtime/import failures, maintainability.
2. `@algo-trading-risk-reviewer` — algorithmic trading logic, CE/PE signal mapping, stops/targets/profit-lock, stale quotes, risk caps, paper/live separation.
3. `@token-spend-safety-reviewer` — cron `no_agent` state, LLM-token budget safety, 15-second loop isolation, 30-minute heartbeat separation, no direct LLM/order-call patterns.
4. `@dashboard-devops-reviewer` — dashboard safety, read-only DB access, safe local binding, UI monitoring usefulness, deployment risks.

Verification commands to run if possible:
- `uv run pytest tests/test_banknifty_options_dashboard.py tests/test_banknifty_options_paper.py -q`
- `uv run python -m py_compile dashboard/banknifty_options_dashboard.py scripts/banknifty_options_paper.py`
- Optional read-only smoke: import dashboard helpers and run `evaluate_system_safety(...)` / `get_db_snapshot()`.

Deliverable:
Write a single markdown report to:
`reports/claude_banknifty_multi_agent_review.md`

Report format:
# Claude multi-agent review: BankNifty paper monitor + dashboard

## Executive summary
- Overall verdict: PASS / PASS WITH FIXES / FAIL
- Top risks in priority order

## Verification actually run
- Commands run and actual results

## Findings by severity
For each finding:
- Severity: Critical / High / Medium / Low / Info
- Area: dashboard / cron / algorithm / storage / safety / tests / deployment
- File/line reference
- What is wrong
- Why it matters
- Recommended fix

## Agent-specific notes
- code-reviewer summary
- algo-trading-risk-reviewer summary
- token-spend-safety-reviewer summary
- dashboard-devops-reviewer summary

## Safe-to-run status
State whether the dashboard is safe to run read-only on localhost and whether the trading monitor remains deterministic/no-agent.

Important: if no issues are found in an area, say what was checked and why it passed. Be adversarial and thorough.
