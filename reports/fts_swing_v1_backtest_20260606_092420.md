## FTS_SWING_V1 Backtest
Version: 1.0
Backtest run ID: 1
Period: 2025-06-05 to 2026-06-05
Universe: /opt/data/finance-db/watchlists/active.csv; Resolution: D
Scope: research-only strategy backtest; not investment advice. No orders placed.

## What this proves
- Tests the technical core of FTS_SWING_V1 on stored FYERS candles.
- Fundamental/sentiment components are wired into the strategy interface.
- Historical fundamental/sentiment evidence is not populated yet, so v1 uses neutral placeholders and flags that limitation.

## Metrics
- total_trades: 340
- wins: 166
- losses: 174
- win_rate_pct: 48.82%
- gross_pnl: ₹1,228.18
- net_pnl: ₹794.27
- ending_equity: ₹5,794.27
- avg_win: ₹35.36
- avg_loss: ₹-29.17
- profit_factor: 1.16
- max_drawdown: ₹1,796.13

## Warnings / limitations
- Fundamental/sentiment historical evidence unavailable; neutral placeholders used for FTS_SWING_V1 v1.0.

## Recent trades
- NSE:BIOCON-EQ
  - Entry: 2026-06-03 at ₹415.21; exit: 2026-06-05 at ₹415.79
  - Qty: 3; Net P&L: ₹0.51; Exit: time_stop; Score: 65.00
- NSE:EXIDEIND-EQ
  - Entry: 2026-06-03 at ₹407.20; exit: 2026-06-05 at ₹402.40
  - Qty: 2; Net P&L: ₹-10.42; Exit: time_stop; Score: 72.00
- NSE:INDUSTOWER-EQ
  - Entry: 2026-06-03 at ₹429.46; exit: 2026-06-05 at ₹430.88
  - Qty: 3; Net P&L: ₹2.97; Exit: time_stop; Score: 72.00
- NSE:SUZLON-EQ
  - Entry: 2026-06-03 at ₹54.55; exit: 2026-06-05 at ₹55.56
  - Qty: 19; Net P&L: ₹18.24; Exit: time_stop; Score: 66.00
- NSE:FEDERALBNK-EQ
  - Entry: 2026-06-04 at ₹301.40; exit: 2026-06-05 at ₹297.80
  - Qty: 4; Net P&L: ₹-15.60; Exit: time_stop; Score: 72.00
- NSE:GMRAIRPORT-EQ
  - Entry: 2026-06-04 at ₹98.06; exit: 2026-06-05 at ₹102.13
  - Qty: 16; Net P&L: ₹63.52; Exit: time_stop; Score: 76.00
- NSE:NMDC-EQ
  - Entry: 2026-06-04 at ₹95.86; exit: 2026-06-05 at ₹92.28
  - Qty: 14; Net P&L: ₹-51.35; Exit: stop_hit; Score: 76.00
- NSE:PIDILITIND-EQ
  - Entry: 2026-06-04 at ₹1,458.13; exit: 2026-06-05 at ₹1,477.36
  - Qty: 1; Net P&L: ₹17.76; Exit: time_stop; Score: 70.00
- NSE:TMPV-EQ
  - Entry: 2026-06-04 at ₹396.40; exit: 2026-06-05 at ₹399.60
  - Qty: 3; Net P&L: ₹8.41; Exit: time_stop; Score: 68.00
- NSE:UPL-EQ
  - Entry: 2026-06-04 at ₹643.07; exit: 2026-06-05 at ₹634.73
  - Qty: 3; Net P&L: ₹-26.93; Exit: time_stop; Score: 70.00

## Next build step
- Populate historical/current fundamental and sentiment evidence tables, then rerun this same backtest with real F+S scores instead of neutral placeholders.
- Only after backtest + paper validation should any live deployment be considered, and then only behind explicit approval.
