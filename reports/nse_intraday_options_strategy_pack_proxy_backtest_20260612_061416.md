# NSE Intraday Options Strategy Pack — Proxy Backtest

Window: 2026-02-01 to 2026-06-11
Mode: paper/proxy only; no live orders; option/spread legs are simulated from underlying 5-minute candles.
Cost model: ₹120 index strategy round-trip, ₹250 stock-option strategy round-trip.

## Summary by strategy
- cpr_trend_debit_spread
  - Trades: 53
  - Wins/Losses: 24/29
  - P&L: ₹-5,206.24
  - Profit factor: 0.88
  - Avg R: -0.06
- expiry_tuesday_directional
  - Trades: 6
  - Wins/Losses: 5/1
  - P&L: ₹3,929.67
  - Profit factor: 3.50
  - Avg R: 0.46
- nifty_orb_debit_spread
  - Trades: 39
  - Wins/Losses: 14/25
  - P&L: ₹-15,748.76
  - Profit factor: 0.51
  - Avg R: -0.28
- nifty_vwap_mean_reversion
  - Trades: 10
  - Wins/Losses: 5/5
  - P&L: ₹249.50
  - Profit factor: 1.03
  - Avg R: 0.02
- single_stock_momentum_index_confirm
  - Trades: 28
  - Wins/Losses: 14/14
  - P&L: ₹12,256.25
  - Profit factor: 1.63
  - Avg R: 0.31

Total trades: 136
Total proxy P&L: ₹-4,519.58
