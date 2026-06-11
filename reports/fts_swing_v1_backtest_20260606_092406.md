## FTS_SWING_V1 Backtest
Version: 1.0
Backtest run ID: not stored
Period: 2025-06-05 to 2026-06-05
Universe: /opt/data/finance-db/watchlists/active.csv; Resolution: D
Scope: research-only strategy backtest; not investment advice. No orders placed.

## What this proves
- Tests the technical core of FTS_SWING_V1 on stored FYERS candles.
- Fundamental/sentiment components are wired into the strategy interface.
- Historical fundamental/sentiment evidence is not populated yet, so v1 uses neutral placeholders and flags that limitation.

## Metrics
- total_trades: 38
- wins: 23
- losses: 15
- win_rate_pct: 60.53%
- gross_pnl: ₹806.22
- net_pnl: ₹763.54
- ending_equity: ₹5,763.54
- avg_win: ₹46.89
- avg_loss: ₹-20.99
- profit_factor: 3.43
- max_drawdown: ₹159.88

## Warnings / limitations
- Fundamental/sentiment historical evidence unavailable; neutral placeholders used for FTS_SWING_V1 v1.0.

## Recent trades
- NSE:ADANIPORTS-EQ
  - Entry: 2026-06-02 at ₹1,746.07; exit: 2026-06-04 at ₹1,790.00
  - Qty: 1; Net P&L: ₹42.16; Exit: time_stop; Score: 65.00
- NSE:ADANIPOWER-EQ
  - Entry: 2026-06-02 at ₹230.12; exit: 2026-06-04 at ₹229.91
  - Qty: 4; Net P&L: ₹-1.72; Exit: time_stop; Score: 76.00
- NSE:360ONE-EQ
  - Entry: 2026-05-27 at ₹1,126.46; exit: 2026-06-01 at ₹1,084.36
  - Qty: 1; Net P&L: ₹-43.21; Exit: stop_hit; Score: 76.00
- NSE:ABCAPITAL-EQ
  - Entry: 2026-05-26 at ₹364.53; exit: 2026-05-29 at ₹363.12
  - Qty: 3; Net P&L: ₹-5.33; Exit: time_stop; Score: 65.00
- NSE:ADANIPOWER-EQ
  - Entry: 2026-05-26 at ₹234.37; exit: 2026-05-29 at ₹243.25
  - Qty: 3; Net P&L: ₹25.93; Exit: time_stop; Score: 70.00
- NSE:AMBUJACEM-EQ
  - Entry: 2026-05-26 at ₹442.27; exit: 2026-05-29 at ₹447.63
  - Qty: 3; Net P&L: ₹14.73; Exit: time_stop; Score: 66.00
- NSE:ATGL-EQ
  - Entry: 2026-05-22 at ₹638.47; exit: 2026-05-26 at ₹702.37
  - Qty: 1; Net P&L: ₹63.23; Exit: target_hit; Score: 70.00
- NSE:360ONE-EQ
  - Entry: 2026-05-20 at ₹1,090.65; exit: 2026-05-22 at ₹1,111.84
  - Qty: 1; Net P&L: ₹20.10; Exit: time_stop; Score: 67.00
- NSE:AUROPHARMA-EQ
  - Entry: 2026-05-19 at ₹1,501.25; exit: 2026-05-21 at ₹1,545.93
  - Qty: 1; Net P&L: ₹43.15; Exit: time_stop; Score: 72.00
- NSE:ADANIPOWER-EQ
  - Entry: 2026-05-18 at ₹221.11; exit: 2026-05-20 at ₹220.13
  - Qty: 4; Net P&L: ₹-4.80; Exit: time_stop; Score: 70.00

## Next build step
- Populate historical/current fundamental and sentiment evidence tables, then rerun this same backtest with real F+S scores instead of neutral placeholders.
- Only after backtest + paper validation should any live deployment be considered, and then only behind explicit approval.
