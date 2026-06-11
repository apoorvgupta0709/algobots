# Multi-Symbol Watchlist Technical Report Implementation Plan

> **For Hermes:** Use subagent-driven-development style: implement in a small verified slice, run tests, then review.

**Goal:** Build a read-only multi-symbol watchlist workflow that refreshes FYERS quotes/candles, computes richer technical factors, and generates a Telegram-friendly daily report.

**Architecture:** Keep the existing `market.candles`, `market.quotes`, and `research.factor_snapshots` tables. Add a simple CSV watchlist loader and orchestration script, extend the pure technical-factor engine, and enhance the report generator to display new evidence-backed fields. No order placement or live execution path.

**Tech Stack:** Python 3.13, psycopg, FYERS v3 scripts, PostgreSQL finance_tracker, pytest.

---

## Task 1: Add watchlist CSV and loader

**Objective:** Create a reusable watchlist file and helper that returns FYERS symbols without duplicating parsing logic.

**Files:**
- Create: `watchlists/default.csv`
- Create: `scripts/watchlist_utils.py`
- Test: `tests/test_watchlist_utils.py`

**Requirements:**
- CSV columns: `symbol,fyers_symbol,company,sector,basket,notes`.
- Default symbols: TVSMOTOR, RELIANCE, HDFCBANK, INFY, TATAMOTORS.
- Loader validates `fyers_symbol` exists and strips blank/comment rows.
- Return a list of dataclass rows plus a convenience `fyers_symbols()` helper.

**Verification:**
- `uv run pytest tests/test_watchlist_utils.py -q`

## Task 2: Extend technical factor engine

**Objective:** Add richer indicators while preserving existing factors and tests.

**Files:**
- Modify: `scripts/compute_technical_factors.py`
- Modify: `tests/test_technical_factors.py`

**Add factors:**
- `sma_200` when enough candles exist.
- `ema_20`.
- `macd_12_26`, `macd_signal_9`, `macd_histogram`.
- `roc_20`, `roc_60`.
- `donchian_20_high`, `donchian_20_low`, `donchian_55_high`, `donchian_55_low`.
- `previous_day_high`, `previous_day_low`, `previous_day_close`.
- `gap_pct` using latest open vs previous close.
- `breakout_20` and `breakout_55` labels: `yes` / `no` / `unknown`.

**Constraints:**
- Keep minimum required candles at 50 for backwards compatibility.
- Only include optional long-window fields when enough history exists.
- Store all numeric values as existing decimal text strings.
- No trade/order language.

**Verification:**
- Existing tests still pass.
- New tests cover EMA/MACD/ROC/Donchian/gap/breakout keys.

## Task 3: Enhance report generator

**Objective:** Display the new technical fields and clearer setup flags.

**Files:**
- Modify: `scripts/generate_daily_market_report.py`
- Modify: `tests/test_daily_market_report.py`

**Requirements:**
- `ReportRow` includes new factor fields.
- Facts section shows MACD histogram, ROC20/60, Donchian20 range, gap %, breakout labels.
- Flags mention breakout watch, gap risk, MACD positive/negative momentum, high volatility, low participation.
- Keep facts separate from suggestions.
- Preserve safety language: read-only, not trade advice, no orders placed.

**Verification:**
- `uv run pytest tests/test_daily_market_report.py -q`

## Task 4: Add orchestration script

**Objective:** One command should refresh a watchlist and write a report.

**Files:**
- Create: `scripts/run_watchlist_daily_report.py`
- Test: `tests/test_watchlist_daily_report.py`
- Modify: `README.md`

**Requirements:**
- Args:
  - `--watchlist watchlists/default.csv`
  - `--resolution D`
  - `--from YYYY-MM-DD` optional; default ~365 calendar days before today.
  - `--to YYYY-MM-DD` optional; default today.
  - `--skip-history`, `--skip-quotes`, `--skip-factors` for testing/retry.
  - `--output PATH`, `--print` forwarded to report generator.
- Internally call existing Python functions where practical:
  - `ingest_fyers_history.run_ingest`
  - `ingest_fyers_quotes.run_ingest`
  - `compute_technical_factors.compute_for_symbols` + `store_factor_snapshots`
  - `generate_daily_market_report.fetch_report_rows/render_report/write_report`
- Set no orders and no execution calls.
- Test with monkeypatches; do not call FYERS in unit tests.

**Verification:**
- `uv run pytest tests/test_watchlist_daily_report.py -q`
- `uv run python scripts/run_watchlist_daily_report.py --help`

## Task 5: End-to-end verification

**Objective:** Prove the slice works.

**Commands:**
- `uv run pytest tests -q`
- If FYERS credentials are available:
  - `FYERS_LOG_PATH=/tmp/ uv run python scripts/run_watchlist_daily_report.py --watchlist watchlists/default.csv --from 2025-10-01 --print`
- Otherwise use skip-ingestion mode against existing DB data:
  - `uv run python scripts/run_watchlist_daily_report.py --watchlist watchlists/default.csv --skip-history --skip-quotes --print`

**Acceptance:**
- Tests pass.
- A Markdown report is written under `reports/`.
- Output says read-only / no orders placed.
