# CLAUDE.md

Guidance for working in this repository. Read this before making changes.

## What this is

Two co-resident subsystems in one repo:

1. **`finance-db` (legacy research system)** — `scripts/`, `dashboard/`,
   `config/`, `migrations/`. A **paper-only** options-trading research system
   for NSE BankNifty / Nifty index options: ingests FYERS API v3 data into
   PostgreSQL, runs deterministic trading logic + proxy backtests, and exposes
   a read-only Streamlit dashboard. Deployed/run from `/opt/data/finance-db/`;
   in this checkout, run scripts relative to the repo root (the wrappers now
   `cd` to their own location).
2. **`algobot/` (platform)** — a 49-strategy backtest/paper/**live-capable**
   NSE trading platform (FastAPI control plane, APScheduler engine, Docker
   Compose). It contains a real FYERS live-order adapter, held behind a hard
   fuse (below). `pyproject.toml` names the project `algobot`.

### Safety — the core invariant, across BOTH subsystems

The **finance-db** subsystem never places live orders and contains no
FYERS order-placement code. Preserve:

- All `config/*.json` ship `paper_only: true` and `live_orders_enabled: false`;
  every `scripts/` config loader **rejects** (SystemExit) any config that flips
  either — keep it.
- Long options only. Option-selling / short-premium structures are blocked.
- The dashboard is **SELECT-only**: dedicated read-only role (`dashboard_ro`,
  migration `009`), `conn.read_only=True`, rejects INSERT/UPDATE/DELETE/DDL, no
  FYERS/LLM/external calls, binds to loopback (`127.0.0.1:8501`) by default.
- `trading.execution_log` requires an explicit `approval_id`; no script writes it.
- `scripts/run_live_order_gate.py` + `config/live_order_gate.json` are dry-run
  scaffolding only.

The **algobot/** subsystem CAN route real orders, so it is fused closed:

- `live_orders_enabled` (`config/settings.yaml`, or env
  `ALGOBOT_LIVE_ORDERS_ENABLED`) defaults **false** and fails closed on any
  malformed value. While closed: the scheduler never wires `FyersBroker`
  (auth success grants live *data* only), `lifecycle.set_mode` refuses
  promotion to LIVE (`force` cannot override), and `FyersBroker.place_order`
  refuses at the lowest layer. Opening the fuse is the explicit, deliberate act
  of enabling real-money trading. See `algobot/core/config.live_orders_enabled`.
- The control API (`algobot/api/`) requires an `ALGOBOT_API_KEY` header on all
  state-changing routes (promote/demote/killswitch/gates and `POST /queries`);
  unset key → 503. `docker-compose.yml` binds api/dashboard to `127.0.0.1` and
  requires `POSTGRES_PASSWORD`.
- `codesfiles/s102_algobotstart.py` is a legacy live bot (places real orders,
  incl. short options); it refuses to run without `ALGOBOT_LEGACY_LIVE_ACK`.

If a task seems to ask for live trading, surface the conflict and confirm —
don't silently open the fuse.

## Tech stack

- Python 3.11+ managed by **uv** (`uv.lock` pins deps; `[tool.uv] package = false`).
- PostgreSQL 17 at `postgresql://hermes@127.0.0.1:55432/finance_tracker`.
  Schemas: `finance`, `market`, `research`, `trading`, `knowledge`.
- Deps: `fyers-apiv3`, `psycopg[binary]`, `python-dotenv`, `streamlit (<2)`.
  Dev: `pytest`.

## Common commands

```bash
uv sync                                   # install deps
uv sync --group dev                       # + pytest (uv-native dev group)
uv run pytest -q                          # whole suite (both subsystems);
                                          # tests needing live Postgres self-skip
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
- `tests/test_*.py` — one unified pytest suite (finance-db + algobot). Most need
  no DB; the few that need live Postgres self-skip via the `requires_finance_db`
  marker (`tests/conftest.py`).
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
- **Paper P&L is spread/cost-aware.** `config/banknifty_options_paper.json` has
  `fills` (entries fill at ask, exits at bid) and `costs` (NSE round-trip stack
  deducted from `realized_pnl`) blocks; both default OFF in code so pure
  functions are unchanged when absent. Backtests are still index-move **proxy**
  backtests — no theta/IV/gamma. See `reports/SUPERSEDED.md` for stale artifacts.
- Cron tick wrappers push a Telegram alert on failure when `TELEGRAM_BOT_TOKEN`
  / `TELEGRAM_CHAT_ID` are set (`scripts/notify_telegram.sh`).
- `docker compose` now requires `POSTGRES_PASSWORD` (no weak default) and binds
  to loopback.

## Before you finish

- Run `uv run pytest -q` — the suite covers the safety rails (paper-only
  enforcement, the algobot live-orders fuse, exit logic, risk caps). Keep it green.
- Re-confirm the finance-db configs still ship paper-only and that the
  `algobot/` `live_orders_enabled` fuse is still closed by default.
