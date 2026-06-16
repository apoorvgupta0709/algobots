# Trading System Roadmap

> Safe operating principle: the system is read-only/research-first. Live order placement remains disabled unless Apoorv explicitly confirms exact order details and risk scope.

## Status legend

- Done: implemented and verified with tests/smoke run.
- In progress: partially built, needs more work before relying on it.
- Pending: not yet built.

## Phase 1 — Research database expansion

Status: Done

Built:

- `migrations/001_trading_research_schemas.sql`
- `knowledge` schema:
  - `sources`
  - `chunks`
  - `concepts`
  - `rules`
  - `playbooks`
- `research` schema:
  - `hypotheses`
  - `strategy_versions`
  - `backtest_runs`
  - `backtest_trades`
  - `factor_snapshots`
  - `model_outputs`
- `trading` schema:
  - `positions_snapshots`
  - `orderbook_snapshots`
  - `holdings_snapshots`
  - `funds_snapshots`
  - `trade_ideas`
  - `approvals`
  - `execution_log`
- Views:
  - `research.latest_strategy_metrics`
  - `trading.open_trade_ideas`

Verification:

- Schema tests pass.
- `trading.execution_log` requires `approval_id`.

## Phase 2 — FYERS read-only trading data

Status: Done

Built:

- `scripts/ingest_fyers_trading_snapshots.py`
- Captures read-only FYERS resources:
  - positions
  - orderbook
  - holdings
  - funds

Verification:

- Unit tests confirm only read-only FYERS methods are called.
- Real FYERS smoke run stored 4 snapshots.

Pending enhancements:

- Add normalized summaries for positions/holdings instead of raw JSON only.
- Add daily/weekly snapshot diff reports.

## Phase 3 — Book / knowledge ingestion pipeline

Status: Pending

Planned:

- Create library directories:
  - `library/books/`
  - `library/notes/`
- Register source documents with file hash.
- Extract text with page/chapter references.
- Chunk text into `knowledge.chunks`.
- Extract trading concepts/rules into `knowledge.concepts` and `knowledge.rules`.
- Add review queue/status workflow.

## Phase 4 — Factor engine and daily symbol analysis

Status: In progress

Done:

- `scripts/compute_technical_factors.py`
- Computes and stores:
  - close
  - SMA20/SMA50
  - RSI14
  - ATR14 / ATR%14
  - relative volume 20
  - trend
  - volatility regime
- Stores snapshots in `research.factor_snapshots`.
- Real FYERS-backed TVSMOTOR daily-candle smoke run completed.

Done in this slice:

- `scripts/generate_daily_market_report.py`
- Generates read-only daily market report from latest quotes + factor snapshots.
- Writes markdown report under `reports/`.

Pending enhancements:

- Add more indicators:
  - MACD
  - ROC
  - support/resistance pivots
  - breakout/failure detection
  - gap analysis
- Add sector/index context.
- Add multi-symbol watchlist config.
- Add daily symbol scoring, but keep it as research labels, not trade commands.

## Phase 5 — Backtesting engine

Status: Pending

Planned:

- Strategy interface.
- Backtest runner over `market.candles`.
- Transaction costs and slippage model.
- Metrics calculator:
  - CAGR / absolute return
  - max drawdown
  - win rate
  - profit factor
  - Sharpe-like risk metric
- Full simulated trade ledger in `research.backtest_trades`.
- Backtest report generator.

## Phase 6 — Reporting and alerts

Status: In progress

Done:

- Manual daily market report script.

Pending:

- Daily quote/candle ingestion workflow for watchlist.
- Daily analysis report cron job.
- Weekly strategy review report.
- Alerts for unusual moves, volume expansion, or risk flags.
- Telegram delivery formatting for reports.

## Phase 7 — Execution guardrails

Status: Partially prepared; live execution still pending/disabled

Done:

- Approval/audit tables exist.
- `execution_log` cannot record execution without explicit approval ID.

Pending before any live order code:

- Define max capital per trade.
- Define max daily loss.
- Define max portfolio exposure.
- Define stop-loss/exit-plan requirements.
- Build dry-run order validator.
- Build approval matcher.
- Build FYERS order-placement wrapper that refuses to run unless approval and risk checks pass.
- Require explicit user confirmation per order.

## Phase 8 — BankNifty day-regime pattern library

Status: Done (deterministic-rules + nearest-neighbour production path); ML experiment-only

Done:

- `scripts/banknifty_trend_patterns.py` — pure feature extraction + deterministic
  classifier (`trend|range|spike_channel|trending_range|reversal|chop`) +
  nearest-neighbour similar-day helpers + paper/research playbook. No DB writes.
- `migrations/016_banknifty_trend_patterns.sql` — `research.banknifty_day_features`,
  `banknifty_day_classifications`, `banknifty_day_pattern_reports` (idempotent;
  class/direction CHECK constraints; `dashboard_ro` SELECT grant).
- `scripts/build_banknifty_trend_pattern_library.py` — backfill/refresh CLI over
  stored 5m index + constituent candles and option-chain summary context.
- `scripts/generate_banknifty_trend_pattern_report.py` — after-market Markdown/JSON
  report (classification, evidence, similar days, how-it-could-have-been-played,
  bot lessons). Runner exit model only (0.5R breakeven + MFE trailing/ratchet);
  never a fixed profit cap.
- `scripts/banknifty_trend_pattern_report.sh` — flock cron wrapper, created but
  NOT scheduled until backfill+report verified across sessions.
- Config-driven thresholds in `config/banknifty_trend_patterns.json`.
- Historical backfill verified: 90 sessions, all six classes represented, missing
  option-chain warned (not guessed), similar-day lists populate as history grows.

Experiment-only (never default until promotion gates pass): clustering (KMeans/
GMM/hierarchical), HMM/sequence states, decision-tree/random-forest, gradient
boosting. No experimental model drives any paper/live decision.

Pending:

- Schedule the cron wrapper (~16:00 IST, Mon–Fri) + Telegram delivery once stable.
- Optionally attach latest day-pattern context into paper-trade `raw` as advisory
  only (must not place orders or override paper safety).

## Current verified test status

- Full test suite: 16 passed (pre-Phase-8 baseline; Phase 8 adds four test files:
  `test_banknifty_trend_patterns.py`, `test_banknifty_trend_pattern_library.py`,
  `test_banknifty_trend_pattern_report.py`, `test_banknifty_trend_pattern_schema.py`).

## Next recommended build order

1. Add watchlist config and multi-symbol ingestion/reporting.
2. Improve technical factor engine with MACD/ROC/support-resistance.
3. Build first simple backtest strategy using stored candles.
4. Add paper-trading trade idea lifecycle.
5. Add scheduled daily report to Telegram.
6. Only then revisit live execution guardrails.
