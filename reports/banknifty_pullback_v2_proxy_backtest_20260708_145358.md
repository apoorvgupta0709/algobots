# BankNifty Pullback Continuation v2 — Proxy Backtest

Research-only; no orders placed.

## Data caveat

Stored 1-min/5-min BankNifty index and constituent candles were used. Historical expired BankNifty option-chain candles are not available in the current FYERS master, so the option leg is simulated with the configured index-to-option beta risk model. Treat this as signal/risk validation, not final option-candle acceptance.

Cost model: ₹100 round-trip per trade (brokerage + slippage), subtracted from every trade.

## Summary
- **Window:** 2020-08-01 to 2026-01-31
- **Trading days:** 973
- **No-trade days:** 973
- **Trades:** 0
- **Win rate:** 0%
- **Total P&L:** ₹0.00
- **Expectancy:** 0R
- **Avg win:** 0R
- **Avg loss:** 0R
- **Avg MFE capture:** 0%
- **Max drawdown:** ₹0.00
- **Days hitting ₹5k cap:** 0
- **Stagnation exits:** 0

## Acceptance gates

- FAIL — ≥ 40 trades
- FAIL — Expectancy ≥ +0.15R
- FAIL — MFE capture ≥ 55%
- PASS — Max DD < 3 daily caps

## Exit counts


## Top rejection counts


Trade CSV: `/opt/data/finance-db/reports/banknifty_pullback_v2_proxy_trades_20260708_145358.csv`
