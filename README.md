# AlgoBot — 49-Strategy Indian Markets Trading Platform

A self-contained NSE algorithmic trading system implementing all 49 strategies
from the *Indian Markets Trading Strategy Compendium* — long-term investing,
swing, intraday, options, and futures/arbitrage — with a shared risk-management
operating system, an event-driven backtester using the real Indian cost stack,
paper trading, gated live deployment, an agent-queryable API, and a monitoring
dashboard.

> Everything ships in **paper mode**. Nothing trades real money until a strategy
> passes the written paper-to-live gate AND you explicitly promote it.

## Quick start (Docker — the Hermes path)

```bash
git clone <this repo> && cd <repo>
cp .env.example .env        # fill in Fyers credentials (optional: paper mode works without)
docker compose up -d --build
```

| Service | Port | What it does |
|---|---|---|
| `engine` | — | APScheduler (Asia/Kolkata): wakes for the 09:15 open, 5-min strategy scans, 15-sec position monitor, 15:15 intraday square-off, EOD/weekly/monthly scans, nightly gate evaluation, stops at close. NSE-holiday aware. |
| `api` | 8000 | FastAPI — REST reads + the agent job queue (below). Self-describing at `GET /`. |
| `dashboard` | 8501 | Streamlit — positions, per-strategy P&L, trade log, gate board, backtests, risk console + kill switch. |
| `db` | — | Postgres (volume-persisted). |

Without Fyers credentials the engine boots in cache-only/paper-only mode and
says so loudly — useful for dry runs.

## Querying the system (Hermes / any agent)

Asynchronous job queue — POST a query, poll the id:

```bash
curl -X POST localhost:8000/queries -H 'content-type: application/json' \
     -d '{"question": "what is my pnl today?"}'      # → {"id": "...", "poll": "/queries/<id>"}
curl localhost:8000/queries/<id>                     # → {"status": "done", "result": {...}}
```

Typed queries: `{"type": "pnl" | "positions" | "trades" | "strategies" |
"strategy_detail" | "gate_status" | "backtests" | "risk" | "status" | "events" |
"promote" | "demote" | "killswitch" | "evaluate_gates", "params": {...}}`.
Direct reads also exist (`GET /status /positions /pnl /strategies /gates /risk`),
plus controls (`POST /strategies/{id}/promote`, `POST /killswitch`).
`GET /` returns the full endpoint catalog and usage guide.

## The lifecycle: build → backtest → paper → live

1. **Backtest** — `python scripts/run_backtest.py --strategy id01_orb --days 250`.
   Event-driven bar replay with the FY2026 Indian cost stack (brokerage, STT,
   exchange, stamp, GST, SEBI fee) + slippage; option legs priced from cached
   real candles when available, else synthetic Black-Scholes (runs are tagged
   `real`/`synthetic` and the gate weights them accordingly).
2. **Paper** — the default mode. Same signals, simulated fills at live quotes
   ± slippage, full trade journal in the DB.
3. **Gate** (compendium §8.2, evaluated nightly): ≥60 paper trades or ≥6 months
   OOS backtest; profit factor ≥1.3 after costs; drawdown inside cap; stop-fire
   fidelity within tolerance. See the dashboard's Gates page.
4. **Live** — explicit promotion only (`POST /strategies/{id}/promote` or the
   dashboard button), which requires an eligible gate. The risk engine sizes
   every order from stop distance (0.5–1% capital), enforces daily (−2.5%) and
   weekly (−5%) loss caps, max concurrent positions, and a kill switch — in
   code, not intention.

## Strategy catalogue (49)

- **Long-term (13)** `lt01–lt13`: index core, SIP, asset-allocation rebalancing,
  Graham value, coffee-can quality, GARP, dividend yield, 12-1 momentum,
  low-volatility, Magic Formula, CANSLIM, special situations, sector rotation.
- **Swing (10)** `sw01–sw10`: Darvas/52-wk breakout, pullback-to-trend, MA
  crossover, Supertrend+ADX, RSI mean reversion, Bollinger squeeze/reversion,
  chart patterns, RS leaders, event swing, pair trading.
- **Intraday (8)** `id01–id08`: ORB, VWAP mean reversion, VWAP trend rides, CPR,
  gap trading, pullback continuation, momentum burst, range fade — index
  strategies execute via defined-risk debit spreads.
- **Options (15)** `op01–op15`: long options, debit/credit verticals, short
  strangle, 9:20 straddle, iron condor/butterfly, covered call, cash-secured
  put, event straddle, calendar, PMCC diagonal, ratio backspread, expiry-day
  playbook, gamma scalping.
- **Futures (3)** `fu01–fu03`: positional trend, cash-futures arbitrage,
  futures calendar spreads.

Per-strategy mode/params/capital: `config/strategies.yaml` (DB overrides via the
API survive restarts). Risk caps: `config/settings.yaml`. Gate thresholds:
`config/gate.yaml`. NSE holidays: `config/nse_holidays.yaml` (update yearly).

## Adding/modifying strategies

One file per strategy, auto-discovered. Read `docs/STRATEGY_AUTHORING.md` —
the contract (pure `generate_signals`, closed bars only, no self-sizing) is
enforced by `tests/test_contract.py` across every registered strategy.

## Development

```bash
pip install -e ".[dev]"
pytest tests/
python scripts/fetch_history.py --symbols NSE:NIFTY50-INDEX --timeframe 5 --days 90
python -m algobot.engine.scheduler --once scan_5min   # single job, no scheduler
streamlit run algobot/dashboard/app.py
```

## Honest limitations

- **Options backtests** largely use synthetic Black-Scholes premiums until the
  live chain recorder builds a real cache — trust paper results over synthetic
  backtests (the gate already discounts them).
- **Fundamentals screens** (`config/fundamentals.csv`) ship with placeholder
  data; plug in a real provider before trusting value/quality/GARP output.
- **Pair trading** backtests are half-hedged (primary leg only); live routes
  both legs. **Cash-futures arb / futures calendars** need live futures quotes
  (`ctx.extras`) and won't trade in backtests.
- The **Fyers headless TOTP login** uses undocumented endpoints that Fyers
  changes occasionally; failures are journaled per-step in the event log.
- `codesfiles/` is the legacy bot, kept for reference only. Files from the
  co-resident `algobots` project (schema.sql, watchlists/, top-level
  dashboard/, backtest_specs/) belong to that sibling project and are not part
  of this platform.

Markets involve risk of loss. This system enforces discipline; it does not
manufacture edge. Verify contract specs, charges and tax rates with your
broker before going live.
