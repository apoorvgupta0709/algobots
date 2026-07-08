# NSE Intraday Options Strategy Pack — Proxy Backtest

Window: 2020-08-01 to 2026-07-08
Mode: paper/proxy only; no live orders; option/spread legs are simulated from underlying 5-minute candles.
Cost model: ₹120 index strategy round-trip, ₹250 stock-option strategy round-trip.

## Summary by strategy
- cpr_trend_debit_spread
  - Trades: 364
  - Wins/Losses: 133/231
  - P&L: ₹-39,193.09
  - Profit factor: 0.83
  - Avg R: -0.08
- expiry_tuesday_directional
  - Trades: 201
  - Wins/Losses: 100/101
  - P&L: ₹-25,072.64
  - Profit factor: 0.83
  - Avg R: -0.08
- nifty_vwap_mean_reversion
  - Trades: 11
  - Wins/Losses: 4/7
  - P&L: ₹-2,363.31
  - Profit factor: 0.73
  - Avg R: -0.15
- single_stock_momentum_index_confirm
  - Trades: 35
  - Wins/Losses: 13/22
  - P&L: ₹5,947.17
  - Profit factor: 1.28
  - Avg R: 0.12

Total trades: 611
Total proxy P&L: ₹-60,681.87
