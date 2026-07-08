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
- total_trades: 27
- wins: 16
- losses: 11
- win_rate_pct: 59.26%
- gross_pnl: ₹370.02
- net_pnl: ₹338.89
- ending_equity: ₹5,338.89
- avg_win: ₹36.98
- avg_loss: ₹-22.99
- profit_factor: 2.34
- max_drawdown: ₹87.52

## Warnings / limitations
- Fundamental/sentiment historical evidence unavailable; neutral placeholders used for FTS_SWING_V1 v1.0.

## Recent trades
- NSE:GMRAIRPORT-EQ
  - Entry: 2026-06-04 at ₹98.06; exit: 2026-06-05 at ₹102.13
  - Qty: 16; Net P&L: ₹63.52; Exit: time_stop; Score: 76.00
- NSE:ETERNAL-EQ
  - Entry: 2026-06-05 at ₹253.48; exit: 2026-06-05 at ₹256.37
  - Qty: 5; Net P&L: ₹13.20; Exit: time_stop; Score: 66.00
- NSE:HINDALCO-EQ
  - Entry: 2026-06-05 at ₹1,127.16; exit: 2026-06-05 at ₹1,092.05
  - Qty: 1; Net P&L: ₹-36.22; Exit: time_stop; Score: 76.00
- NSE:NATIONALUM-EQ
  - Entry: 2026-06-05 at ₹412.71; exit: 2026-06-05 at ₹396.01
  - Qty: 3; Net P&L: ₹-51.30; Exit: stop_hit; Score: 72.00
- NSE:ADANIPOWER-EQ
  - Entry: 2026-06-02 at ₹230.12; exit: 2026-06-04 at ₹229.91
  - Qty: 4; Net P&L: ₹-1.72; Exit: time_stop; Score: 76.00
- NSE:DRREDDY-EQ
  - Entry: 2026-05-29 at ₹1,319.46; exit: 2026-06-02 at ₹1,279.54
  - Qty: 1; Net P&L: ₹-41.22; Exit: stop_hit; Score: 67.00
- NSE:SAIL-EQ
  - Entry: 2026-05-26 at ₹198.45; exit: 2026-05-29 at ₹204.27
  - Qty: 4; Net P&L: ₹22.47; Exit: time_stop; Score: 76.00
- NSE:MOTHERSON-EQ
  - Entry: 2026-05-22 at ₹137.37; exit: 2026-05-26 at ₹135.75
  - Qty: 8; Net P&L: ₹-14.02; Exit: time_stop; Score: 76.00
- NSE:POLICYBZR-EQ
  - Entry: 2026-05-20 at ₹1,772.09; exit: 2026-05-22 at ₹1,791.50
  - Qty: 1; Net P&L: ₹17.64; Exit: time_stop; Score: 76.00
- NSE:DRREDDY-EQ
  - Entry: 2026-05-18 at ₹1,337.17; exit: 2026-05-20 at ₹1,321.24
  - Qty: 1; Net P&L: ₹-17.26; Exit: time_stop; Score: 76.00

## Next build step
- Populate historical/current fundamental and sentiment evidence tables, then rerun this same backtest with real F+S scores instead of neutral placeholders.
- Only after backtest + paper validation should any live deployment be considered, and then only behind explicit approval.
