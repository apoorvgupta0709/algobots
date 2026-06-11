# BankNifty Options Paper Monitor - Dashboard Plan

## Goal
Build a read-only live dashboard for Apoorv to monitor the deterministic paper-trading system without adding LLM calls to the trading loop.

## Current source of truth
- Cron metadata: `/opt/data/profiles/finance/cron/jobs.json`
- Runtime config: `/opt/data/finance-db/config/banknifty_options_paper.json`
- PostgreSQL: `finance_tracker` on `127.0.0.1:55432`
- Main tables:
  - `market.quotes` - latest FYERS quote per symbol
  - `research.option_contracts` - BankNifty option contract master
  - `research.option_paper_campaigns` - paper campaign config/state
  - `research.option_paper_trades` - paper option trades and P&L
  - `research.option_paper_trade_events` - open/stop/close event log
  - `research.option_paper_daily_snapshots` - EOD equity snapshots

## Recommended dashboard stack
Start with Streamlit because it is fastest and low-risk:
- one Python file: `dashboard/banknifty_options_dashboard.py`
- read-only SQL connection
- auto-refresh every 15-30 seconds
- run locally/VPS-bound behind VPN or SSH tunnel first

Later, upgrade to FastAPI + React if needed.

## Dashboard sections

### 1. System health
Show:
- monitor job enabled: yes/no
- monitor `no_agent`: must be true
- monitor schedule: `* * * * 1-5`
- heartbeat schedule: `0,30 4-10 * * 1-5`
- drift guard last status
- config safety: `paper_only=true`, `live_orders_enabled=false`

### 2. Live market state
Show:
- BankNifty index LTP, open, % from open, quote updated time
- constituent coverage %
- top positive contributors
- top negative contributors
- quote freshness warning if stale > 90 sec

### 3. Current position
Show:
- open trade count
- symbol, CE/PE, strike, expiry
- entry premium, current LTP, stop, target, highest premium
- live paper P&L
- time in trade

### 4. Risk caps
Show:
- starting capital: ₹50,000
- max daily loss: ₹5,000
- max trade loss: ₹1,500
- max trades/day: 4
- max open positions: 1
- trades used today
- realized P&L today

### 5. Event log
Show latest rows from `research.option_paper_trade_events`:
- event time
- event type
- message
- premium
- quantity

### 6. Equity curve
Show from `research.option_paper_daily_snapshots`:
- date
- realized P&L
- unrealized P&L
- equity

## Safety rules for dashboard
- The dashboard must be read-only.
- It must never call FYERS order APIs.
- It must never call an LLM.
- It should only query PostgreSQL and read cron/config files.
- It can show a red warning if the monitor is not `no_agent=true` or if live orders are enabled.

## Useful SQL snippets

### Open trades
```sql
select
  option_trade_id, symbol, option_type, expiry, strike,
  entry_premium, stop_premium, target_premium, highest_premium,
  quantity, premium_value, entry_time, strategy_version, raw
from research.option_paper_trades
where status='open'
order by entry_time desc;
```

### Latest events
```sql
select event_time, event_type, premium, quantity, message
from research.option_paper_trade_events
order by event_time desc
limit 50;
```

### Daily snapshot
```sql
select snapshot_date, starting_capital, realized_pnl, unrealized_pnl, equity,
       open_positions, closed_positions
from research.option_paper_daily_snapshots
order by snapshot_date desc;
```

### BankNifty quote freshness
```sql
select symbol, ltp, open, high, low, close, volume, quote_time, updated_at,
       extract(epoch from (now() - updated_at)) as age_seconds
from market.quotes
where symbol='NSE:NIFTYBANK-INDEX';
```

## Suggested build phases
1. Build local Streamlit dashboard with read-only SQL and cron/config panels.
2. Add auto-refresh and color-coded alerts.
3. Add charts: intraday paper P&L, daily equity, quote freshness.
4. Put behind SSH tunnel or authenticated reverse proxy before exposing externally.
5. Optional: add Telegram summary buttons later, still read-only.
