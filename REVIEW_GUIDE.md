# Review Guide

How to review the trading logic in this repo, in order. Written for Claude / any external reviewer.

## 0. What this repo is

A paper-only NSE options trading research system (FYERS data, Postgres storage, Python + uv). Two active engines:

1. **BankNifty options paper engine** — constituent-led pullback-continuation long options (`pullback_continuation_v2`).
2. **NSE intraday options strategy pack** — 5 deterministic intraday strategies (ORB debit spread, CPR trend-day debit spread, expiry-Tuesday directional, VWAP mean reversion, single-stock momentum with index confirmation).

**Nothing places live orders.** Every config ships with `paper_only: true` and `live_orders_enabled: false`, and the live-order gate defaults to dry-run.

## 1. Start with the strategy cards

- `docs/strategy-cards/README.md` — list of all cards.
- `docs/strategy-cards/Strategy Cards Index.md` — index/status of each card.
- `docs/source-notes/` — where the strategies came from and why they exist.

## 2. Read the configs (active thresholds and safety rails)

| File | What it controls |
|---|---|
| `config/banknifty_options_paper.json` | BankNifty engine: ₹50,000 campaign capital, ₹1,500 max loss/trade, ₹5,000 daily loss cap, max 3 trades/day, 1 open position, ₹40,000 max premium exposure, entry windows 09:35–14:45, force exit 15:20, spread filter, strategy router (only constituent-led long options is paper-enabled; short-premium structures are blocked) |
| `config/nse_intraday_options_strategy_pack.json` | 5-strategy pack: per-strategy ₹50,000 paper capital, ₹1,500 max loss/trade, ₹5,000 daily loss cap, ₹40,000 premium exposure, 3 trades/day, 1 open position each; global max 5 open positions; force exit 15:20 |
| `config/live_order_gate.json` | Live-order safety gate: `live_orders_enabled: false`, kill switch on, CNC/LIMIT only, ₹50 max risk/trade — dry-run scaffold, no FYERS order code active |
| `config/paper_algobot.json` | Phase 2A equity paper bot (₹5,000 capital, ₹50 risk/trade) |

## 3. Inspect the core strategy code

Read in this order:

1. `scripts/banknifty_options_paper.py` — BankNifty paper engine: constituent/index confirmation, entry scan, risk caps, exits (structural SL, premium hard SL, breakeven/MFE ratchet, stagnation, force exit), event logging.
2. `scripts/nse_intraday_options_strategy_pack.py` — pure strategy logic for the 5-strategy pack.
3. `scripts/run_nse_intraday_options_strategy_pack.py` — backtest / scan / tick runner for the pack.
4. `scripts/run_banknifty_pullback_v2_backtest.py` — BankNifty pullback-continuation proxy backtest runner.
5. `scripts/run_live_order_gate.py` — how live orders are blocked unless explicitly enabled.

Review questions to keep in mind:

- Do the coded risk caps match the JSON values (max trade loss, daily loss, exposure, trade counts)?
- Are exits enforced unconditionally (force exit 15:20, daily-loss halt)?
- Any look-ahead bias in entry scans or backtests?
- Is the live-order path truly unreachable while `live_orders_enabled` is false?

## 4. Run the tests

```bash
uv sync
uv run pytest -q
```

Last verified locally: **184 passed**. Key files:

- `tests/test_banknifty_options_paper.py`
- `tests/test_nse_intraday_options_strategy_pack.py`
- `tests/test_live_order_gate.py`
- `tests/test_strategy_card_ingestion.py`
- `tests/test_banknifty_options_dashboard.py`

## 5. Review the backtest / tuning evidence

- `reports/nse_intraday_options_strategy_pack_proxy_backtest_20260611_141323.md` (+ matching `_trades_*.csv`)
- `reports/banknifty_pullback_v2_proxy_backtest_*.md` (+ `_trades_*.csv`)
- `reports/banknifty_pullback_v2_exit_tuning_*.json` / `.csv`
- `reports/claude_banknifty_multi_agent_review.md` — prior multi-agent review findings
- `reports/claude_banknifty_review_handoff_prompt.md` — review handoff context

These are **proxy** backtests (index-move proxies for option P&L), not full option-chain replays — treat the numbers as directional, not exact.

## 6. Supporting context

- `migrations/008_banknifty_options_paper.sql`, `migrations/012_nse_intraday_options_strategy_pack.sql` — paper portfolio / campaign / trade-event schema; `009_dashboard_readonly_role.sql` for the dashboard role.
- `dashboard/banknifty_options_dashboard.py` + `reports/banknifty_options_dashboard_plan.md` — read-only monitor.
- `reports/banknifty_options_monitor_flow.excalidraw`, `reports/trading_system_architecture/` — architecture diagrams.
- `data/index_constituents/`, `watchlists/*.csv` — symbol universe and BankNifty constituent weights.

## 7. What is intentionally NOT in this repo

- `.env`, FYERS tokens/secrets, GitHub keys
- Postgres runtime (`pgdata/`, `pgroot/`), live DB state, historical candle DB
- Runtime logs, `.venv`, caches, raw PDFs/books, Hermes cron internals

Consequence: you can review all logic and run the unit tests, but you **cannot replay live paper-trading state** without a sanitized DB/candle snapshot.
