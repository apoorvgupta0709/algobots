## FTS_SWING_V1 Backtest
Version: 1.0
Backtest run ID: 6
Period: 2025-06-05 to 2026-06-05
Universe: /opt/data/finance-db/watchlists/active.csv; Resolution: D
Scope: research-only strategy backtest; not investment advice. No orders placed.

## What this proves
- Tests the technical core of FTS_SWING_V1 on stored FYERS candles.
- Fundamental/sentiment components are wired into the strategy interface.
- Historical fundamental/sentiment evidence is not populated yet, so v1 uses neutral placeholders and flags that limitation.

## Metrics
- total_trades: 13
- wins: 9
- losses: 4
- win_rate_pct: 69.23%
- gross_pnl: ₹365.96
- net_pnl: ₹352.18
- ending_equity: ₹5,352.18
- avg_win: ₹45.77
- avg_loss: ₹-14.93
- profit_factor: 6.90
- max_drawdown: ₹50.96

## Warnings / limitations
- Fundamental/sentiment historical evidence unavailable; neutral placeholders used for FTS_SWING_V1 v1.0.

## Recent trades
- NSE:ADANIPOWER-EQ
  - Entry: 2026-06-02 at ₹230.12; exit: 2026-06-04 at ₹229.91
  - Qty: 4; Net P&L: ₹-1.72; Exit: time_stop; Score: 76.00
- NSE:ADANIPOWER-EQ
  - Entry: 2026-05-26 at ₹234.37; exit: 2026-05-29 at ₹243.25
  - Qty: 3; Net P&L: ₹25.93; Exit: time_stop; Score: 70.00
- NSE:360ONE-EQ
  - Entry: 2026-05-20 at ₹1,090.65; exit: 2026-05-22 at ₹1,111.84
  - Qty: 1; Net P&L: ₹20.10; Exit: time_stop; Score: 67.00
- NSE:ADANIPOWER-EQ
  - Entry: 2026-05-18 at ₹221.11; exit: 2026-05-20 at ₹220.13
  - Qty: 4; Net P&L: ₹-4.80; Exit: time_stop; Score: 70.00
- NSE:360ONE-EQ
  - Entry: 2026-05-14 at ₹1,080.64; exit: 2026-05-18 at ₹1,104.35
  - Qty: 1; Net P&L: ₹22.62; Exit: time_stop; Score: 65.00
- NSE:ADANIPORTS-EQ
  - Entry: 2026-05-12 at ₹1,752.88; exit: 2026-05-12 at ₹1,703.65
  - Qty: 1; Net P&L: ₹-50.96; Exit: stop_hit; Score: 68.00
- NSE:360ONE-EQ
  - Entry: 2026-05-07 at ₹1,084.64; exit: 2026-05-11 at ₹1,114.74
  - Qty: 1; Net P&L: ₹29.00; Exit: time_stop; Score: 70.00
- NSE:ABCAPITAL-EQ
  - Entry: 2026-04-30 at ₹346.17; exit: 2026-05-05 at ₹360.67
  - Qty: 4; Net P&L: ₹56.57; Exit: time_stop; Score: 76.00
- NSE:ADANIPOWER-EQ
  - Entry: 2026-04-20 at ₹198.60; exit: 2026-04-22 at ₹215.54
  - Qty: 5; Net P&L: ₹83.68; Exit: time_stop; Score: 72.00
- NSE:ADANIENSOL-EQ
  - Entry: 2026-04-13 at ₹1,120.56; exit: 2026-04-16 at ₹1,221.99
  - Qty: 1; Net P&L: ₹100.26; Exit: time_stop; Score: 80.00

## Next build step
- Populate historical/current fundamental and sentiment evidence tables, then rerun this same backtest with real F+S scores instead of neutral placeholders.
- Only after backtest + paper validation should any live deployment be considered, and then only behind explicit approval.
