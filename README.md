# Local finance PostgreSQL + FYERS v3 ingestion

## What is installed

- PostgreSQL 17.10, installed without root under `/opt/data/finance-db/pgroot`
- Data directory: `/opt/data/finance-db/pgdata`
- Database: `finance_tracker`
- Connection: `postgresql://hermes@127.0.0.1:55432/finance_tracker`
- Schemas:
  - `finance`: accounts + transactions
  - `market`: instruments, candles, quotes, ingestion run audit log

## Start / stop / connect

```bash
/opt/data/finance-db/scripts/start-postgres.sh
/opt/data/finance-db/scripts/stop-postgres.sh
/opt/data/finance-db/scripts/psql.sh
```

## FYERS credentials

Copy `/opt/data/finance-db/.env.example` values into `/opt/data/finance-db/.env` locally.
Do not paste FYERS secrets or access tokens into chat.

Required values:

```bash
FYERS_CLIENT_ID=...
FYERS_SECRET_KEY=...
FYERS_REDIRECT_URI=...
FYERS_ACCESS_TOKEN=...
```

Generate auth URL:

```bash
cd /opt/data/finance-db
.venv/bin/python scripts/fyers_auth.py auth-url
```

After logging in via that URL and copying the redirect `auth_code` locally:

```bash
.venv/bin/python scripts/fyers_auth.py token --auth-code '<auth_code>'
```

Put the returned access token in `.env` as `FYERS_ACCESS_TOKEN`.

## Ingest historical candles

```bash
cd /opt/data/finance-db
.venv/bin/python scripts/ingest_fyers_history.py \
  --symbols NSE:SBIN-EQ NSE:RELIANCE-EQ \
  --resolution D \
  --from 2024-01-01 \
  --to 2024-12-31
```

## Ingest latest quotes

```bash
cd /opt/data/finance-db
.venv/bin/python scripts/ingest_fyers_quotes.py --symbols NSE:SBIN-EQ NSE:RELIANCE-EQ
```

## Useful SQL

```sql
select * from market.latest_candles order by symbol;

select symbol, resolution, count(*) as candles, min(ts) as first_ts, max(ts) as last_ts
from market.candles
group by symbol, resolution
order by symbol, resolution;

select * from market.ingestion_runs order by started_at desc limit 20;
```

## Trading research system

Phase 1/2 safety baseline is now available:

- `migrations/001_trading_research_schemas.sql` creates:
  - `knowledge`: sources, chunks, concepts, rules, playbooks
  - `research`: hypotheses, strategy versions, backtest runs/trades, factors, model outputs
  - `trading`: read-only account snapshots, trade ideas, approvals, execution audit
- Live execution remains disabled-by-default. `trading.execution_log` requires an explicit `approval_id`; no order-placement script exists.

Apply/refresh the idempotent schema migration:

```bash
cd /opt/data/finance-db
./scripts/psql.sh -h 127.0.0.1 -p 55432 -d finance_tracker -f migrations/001_trading_research_schemas.sql
```

Capture FYERS read-only trading snapshots:

```bash
cd /opt/data/finance-db
uv run python scripts/ingest_fyers_trading_snapshots.py

# Optional subset
uv run python scripts/ingest_fyers_trading_snapshots.py --resources positions holdings
```

The snapshot script only calls FYERS read-only endpoints: positions, orderbook, holdings, and funds.

## Technical factor engine

`/opt/data/finance-db/scripts/compute_technical_factors.py` computes the latest technical snapshot from stored `market.candles` and upserts it into `research.factor_snapshots` with source `technical_factor_engine`.

Current factors:

- `close`
- `sma_20`, `sma_50`, `sma_200`
- `ema_20`
- `rsi_14`
- `atr_14`, `atr_pct_14`
- `relative_volume_20` — latest volume vs prior 20-candle average
- `macd_12_26`, `macd_signal_9`, `macd_histogram`
- `roc_20`, `roc_60`
- `donchian_20_high`, `donchian_20_low`, `donchian_55_high`, `donchian_55_low`
- `previous_day_high`, `previous_day_low`, `previous_day_close`, `gap_pct`
- `breakout_20`, `breakout_55` — close above prior 20/55-day high range
- `trend` — bullish/bearish/neutral from close vs SMA 20/50
- `volatility_regime` — low/normal/high from ATR percentage

Example: ingest candles, then compute factors for TVS Motor:

```bash
cd /opt/data/finance-db
FYERS_LOG_PATH=/tmp/ uv run python scripts/ingest_fyers_history.py \
  --symbols NSE:TVSMOTOR-EQ \
  --resolution D \
  --from 2025-10-01 \
  --to 2026-06-02

uv run python scripts/compute_technical_factors.py \
  --symbols NSE:TVSMOTOR-EQ \
  --resolution D
```

Compute all symbols/resolutions with at least 50 stored candles:

```bash
uv run python scripts/compute_technical_factors.py --resolution D
```

Query stored factors:

```sql
select symbol, resolution, ts,
       factors->>'close' as close,
       factors->>'trend' as trend,
       factors->>'rsi_14' as rsi_14,
       factors->>'atr_pct_14' as atr_pct_14
from research.factor_snapshots
where source = 'technical_factor_engine'
order by ts desc, symbol;
```

## Daily market report

`/opt/data/finance-db/scripts/generate_daily_market_report.py` creates a read-only Markdown report from latest quotes and `research.factor_snapshots`.

Generate and print a report for TVS Motor:

```bash
cd /opt/data/finance-db
FYERS_LOG_PATH=/tmp/ uv run python scripts/ingest_fyers_quotes.py --symbols NSE:TVSMOTOR-EQ
uv run python scripts/generate_daily_market_report.py --symbols NSE:TVSMOTOR-EQ --print
```

The report is written to:

```bash
/opt/data/finance-db/reports/daily_market_report_YYYY-MM-DD.md
```

It separates facts, data freshness, risk flags/setups, and suggested next actions. Use `--resolution D` when you want only daily factor snapshots; the watchlist orchestrator passes its selected resolution automatically. It does not place orders or provide trade execution instructions.

## Watchlist daily report

`/opt/data/finance-db/watchlists/default.csv` is the default multi-symbol watchlist. It uses columns:

```csv
symbol,fyers_symbol,company,sector,basket,notes
```

Refresh FYERS history/quotes, compute factors, and write one read-only report. The default lookback is ~365 calendar days so SMA200 has enough daily candles when FYERS auth/data are available:

```bash
cd /opt/data/finance-db
FYERS_LOG_PATH=/tmp/ uv run python scripts/run_watchlist_daily_report.py \
  --watchlist watchlists/default.csv \
  --from 2025-06-03 \
  --resolution D \
  --print
```

Retry/report from already-ingested database data without calling FYERS:

```bash
uv run python scripts/run_watchlist_daily_report.py \
  --watchlist watchlists/default.csv \
  --skip-history \
  --skip-quotes \
  --print
```

This orchestrator is also read-only: it only ingests market data, computes research factors, and writes Markdown reports. It has no order-placement or execution path.

## Book knowledge base

Drop trading-book PDFs into `books/` (VPS: `/opt/data/finance-db/books/`), then:

```bash
# one-time on the VPS: make pgvector available, then apply migration 014
./scripts/install_pgvector.sh
./scripts/psql.sh -f migrations/014_knowledge_embeddings.sql

# ingest (idempotent — re-running skips unchanged books)
uv run python scripts/ingest_books.py

# search with citations (read-only; --json for agents)
uv run python scripts/query_knowledge.py "position sizing for index options" --top-k 8
```

Embeddings are computed locally (BAAI/bge-small-en-v1.5 on CPU; first run
downloads the model to `~/.cache`). Image-only/scanned PDFs are registered
with `needs_ocr` and skipped. Config: `config/knowledge_ingestion.json`.
Workflow from book knowledge to a live-trading shortlist:
`docs/plans/book-to-live-strategy-playbook.md`.

## Roadmap

Full roadmap with done/pending status:

```bash
/opt/data/finance-db/docs/plans/trading-system-roadmap.md
```
