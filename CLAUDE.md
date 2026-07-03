# CLAUDE.md

Guidance for working in this repository. Read this before making changes.

## What this is

A **paper-only** options-trading research system for NSE BankNifty / Nifty index
options. It ingests FYERS API v3 market data into PostgreSQL, runs deterministic
trading logic + backtests, and exposes a read-only Streamlit dashboard.

`pyproject.toml` names the project `finance-db`. The repo is deployed/run from
`/opt/data/finance-db/` — README.md and the shell wrappers use that absolute
path. In this checkout, run scripts relative to the repo root.

### Safety is the core invariant — do not break it

This system **never places live orders**. No FYERS order-placement code exists.
When changing anything, preserve these guarantees:

- All configs ship with `paper_only: true` and `live_orders_enabled: false`.
  Config validation **rejects** any config that sets either otherwise — keep it.
- Long options only. Option-selling / short-premium structures are blocked.
- The dashboard is **SELECT-only**: it connects via a dedicated read-only DB role
  (`dashboard_ro`, migration `009`), sets the connection read-only, rejects
  INSERT/UPDATE/DELETE/DDL, makes no FYERS calls, no LLM/external calls, and binds
  to loopback (`127.0.0.1:8501`) by default.
- `trading.execution_log` requires an explicit `approval_id`; there is no script
  that writes to it.
- `scripts/run_live_order_gate.py` + `config/live_order_gate.json` are dry-run
  scaffolding only (`live_orders_enabled: false`).

If a task seems to ask for live trading, surface the conflict — don't silently
enable it.

## Tech stack

- Python 3.11+ managed by **uv** (`uv.lock` pins deps; `[tool.uv] package = false`).
- PostgreSQL 17 at `postgresql://hermes@127.0.0.1:55432/finance_tracker`.
  Schemas: `finance`, `market`, `research`, `trading`, `knowledge`.
- Deps: `fyers-apiv3`, `psycopg[binary]`, `python-dotenv`, `streamlit (<2)`.
  Dev: `pytest`.

## Common commands

```bash
uv sync                                   # install deps
uv sync --group dev                       # + pytest
uv run pytest -q                          # run the test suite
uv run pytest tests/test_banknifty_options_paper.py -v   # one file

# PostgreSQL lifecycle (wrappers assume the /opt/data/finance-db deploy path)
./scripts/start-postgres.sh
./scripts/stop-postgres.sh
./scripts/psql.sh                         # psql shell
./scripts/psql.sh -f migrations/001_trading_research_schemas.sql   # apply a migration
```

### Running the engines (paper only)

```bash
# BankNifty paper engine — intraday tick loop (runs ~09:20–15:20 IST, Mon–Fri)
FYERS_LOG_PATH=/tmp/ uv run python scripts/banknifty_options_paper.py \
  --mode tick --refresh-quotes --quiet-no-change --loop-seconds 55
# modes: tick (live monitor), report (EOD snapshot), scan (one-shot)

# Cron wrappers (handle flock + PostgreSQL autostart)
./scripts/banknifty_options_paper_tick.sh
./scripts/banknifty_options_eod_report.sh
./scripts/banknifty_options_contract_refresh.sh   # one-time contract master refresh

# NSE intraday 5-strategy pack
uv run python scripts/run_nse_intraday_options_strategy_pack.py --mode backtest \
  --from 2025-06-01 --to 2026-06-11 --config config/nse_intraday_options_strategy_pack.json

# Backtests are PROXY backtests (index-move P&L), not option-chain replay
uv run python scripts/run_banknifty_pullback_v2_backtest.py --from 2025-06-01 --to 2026-06-11

# Read-only dashboard
./scripts/run_banknifty_options_dashboard.sh      # http://127.0.0.1:8501
```

### Data ingestion & analysis

```bash
FYERS_LOG_PATH=/tmp/ uv run python scripts/ingest_fyers_history.py \
  --symbols NSE:AXISBANK-EQ --resolution D --from 2025-06-01 --to 2026-06-11
uv run python scripts/ingest_fyers_quotes.py --symbols NSE:AXISBANK-EQ
uv run python scripts/ingest_fyers_trading_snapshots.py   # read-only FYERS state only
uv run python scripts/compute_technical_factors.py --resolution D
uv run python scripts/run_watchlist_daily_report.py --watchlist watchlists/default.csv --print

# BankNifty daywise trend-pattern library (research/paper-only; deterministic
# rules + nearest-neighbour similar-day library; no orders)
uv run python scripts/build_banknifty_trend_pattern_library.py \
  --from 2025-06-01 --to 2026-06-16 --resolution 5 --print   # add --dry-run to skip writes
uv run python scripts/generate_banknifty_trend_pattern_report.py --date 2026-06-16 --print
./scripts/banknifty_trend_pattern_report.sh                  # cron wrapper (NOT scheduled yet)
```

Day-pattern notes: classes are `trend|range|spike_channel|trending_range|reversal|chop`;
all thresholds live in `config/banknifty_trend_patterns.json`; missing option-chain
context is warned, never guessed; ML/HMM/tree/boosting stay experiment-only roadmap.
Report "how it could have been played" / "bot lessons" use the runner exit model
(0.5R breakeven + MFE trailing/ratchet) — never a fixed profit cap.

## Layout

- `scripts/` — all entry points (Python + `.sh` cron wrappers). The BankNifty
  engine `scripts/banknifty_options_paper.py` is the largest/most important file.
- `dashboard/banknifty_options_dashboard.py` — read-only Streamlit monitor.
- `config/*.json` — strategy params. **No thresholds are hardcoded**; everything
  is config-driven.
- `migrations/*.sql` — idempotent PostgreSQL schema, applied in number order.
- `tests/test_*.py` — pytest suite, ~1:1 with `scripts/*.py`. No DB needed; inputs
  are hardcoded/sample rows.
- `watchlists/*.csv` — symbol universes (`symbol,fyers_symbol,company,sector,basket,notes`).
- `docs/strategy-cards/` — Markdown research cards per strategy. `docs/plans/` — roadmap.
- `reports/` — generated Markdown/CSV output (timestamped filenames).
- `REVIEW_GUIDE.md` — review checklist for trading logic; read it before reviewing.

## Conventions

- **Use `Decimal` for all money** (`ROUND_HALF_UP`), never float. Quantize to `0.01`.
- **Timezone:** trading logic is IST-aware (`ZoneInfo("Asia/Kolkata")`).
- `from __future__ import annotations`, dataclasses, full type hints throughout.
- Config validation raises `ValueError` on unsafe configs — extend, don't bypass.
- All strategy decisions are logged to PostgreSQL (`research.option_*` tables),
  the source of truth — not local files.
- Secrets live only in `.env` (copy `.env.example`); never commit tokens. Do not
  paste FYERS secrets/tokens into chat.

## Before you finish

- Run `uv run pytest -q` — the suite covers the safety rails (paper-only enforcement,
  exit logic, risk caps). Keep it green.
- Re-confirm no config or code path enables live orders.
