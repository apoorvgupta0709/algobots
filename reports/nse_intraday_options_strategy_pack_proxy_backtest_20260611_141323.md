# NSE Intraday Options Strategy Pack — Proxy Backtest

Window: 2026-02-01 to 2026-06-11
Mode: paper/proxy only; no live orders; option/spread legs are simulated from underlying 5-minute candles.
Cost model: ₹120 index strategy round-trip, ₹250 stock-option strategy round-trip.

## Summary by strategy
- cpr_trend_debit_spread
  - Trades: 49
  - Wins/Losses: 21/28
  - P&L: ₹4,411.41
  - Profit factor: 1.11
  - Avg R: 0.07
- expiry_tuesday_directional
  - Trades: 6
  - Wins/Losses: 5/1
  - P&L: ₹3,929.67
  - Profit factor: 3.50
  - Avg R: 0.46
- nifty_orb_debit_spread
  - Trades: 43
  - Wins/Losses: 15/28
  - P&L: ₹-14,555.45
  - Profit factor: 0.59
  - Avg R: -0.23
- nifty_vwap_mean_reversion
  - Trades: 25
  - Wins/Losses: 12/13
  - P&L: ₹-1,140.41
  - Profit factor: 0.94
  - Avg R: -0.03
- single_stock_momentum_index_confirm
  - Trades: 33
  - Wins/Losses: 13/20
  - P&L: ₹-3,418.64
  - Profit factor: 0.89
  - Avg R: -0.08

Total trades: 156
Total proxy P&L: ₹-10,773.42
