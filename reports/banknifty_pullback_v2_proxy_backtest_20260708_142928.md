# BankNifty Pullback Continuation v2 — Proxy Backtest

Research-only; no orders placed.

## Data caveat

Stored 1-min/5-min BankNifty index and constituent candles were used. Historical expired BankNifty option-chain candles are not available in the current FYERS master, so the option leg is simulated with the configured index-to-option beta risk model. Treat this as signal/risk validation, not final option-candle acceptance.

Cost model: ₹100 round-trip per trade (brokerage + slippage), subtracted from every trade.

## Summary
- **Window:** 2026-02-01 to 2026-07-08
- **Trading days:** 106
- **No-trade days:** 50
- **Trades:** 87
- **Win rate:** 39.08%
- **Total P&L:** ₹3,476.35
- **Expectancy:** 0.03R
- **Avg win:** 0.98R
- **Avg loss:** -0.58R
- **Avg MFE capture:** 25.22%
- **Max drawdown:** ₹-6,389.19
- **Days hitting ₹5k cap:** 0
- **Stagnation exits:** 0

## Acceptance gates

- PASS — ≥ 40 trades
- FAIL — Expectancy ≥ +0.15R
- FAIL — MFE capture ≥ 55%
- PASS — Max DD < 3 daily caps

## Exit counts

- index_structure_stop: 26
- mfe_ratchet_stop: 61

## Top rejection counts

- breadth/index disagreement: 1309
- pullback_not_confirmed: 1099
- lunch_chop: 710
- top mover VWAP/rel-vol confirmation missing: 424
- chop_regime: 366
- risk_over_cap: 45
- burned_level: 21
- weighted VWAP side 51.65% < 60%: 16
- weighted VWAP side 58.16% < 60%: 12
- weighted VWAP side 50.44% < 60%: 11
- weighted VWAP side 0.00% < 60%: 11
- weighted VWAP side 30.14% < 60%: 11

Trade CSV: `/opt/data/finance-db/reports/banknifty_pullback_v2_proxy_trades_20260708_142928.csv`
