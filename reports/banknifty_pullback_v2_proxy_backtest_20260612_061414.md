# BankNifty Pullback Continuation v2 — Proxy Backtest

Research-only; no orders placed.

## Data caveat

Stored 1-min/5-min BankNifty index and constituent candles were used. Historical expired BankNifty option-chain candles are not available in the current FYERS master, so the option leg is simulated with the configured index-to-option beta risk model. Treat this as signal/risk validation, not final option-candle acceptance.

## Summary
- **Window:** 2026-02-01 to 2026-06-11
- **Trading days:** 88
- **No-trade days:** 34
- **Trades:** 77
- **Win rate:** 61.04%
- **Total P&L:** ₹13,974.35
- **Expectancy:** 0.13R
- **Avg win:** 0.86R
- **Avg loss:** -1.00R
- **Avg MFE capture:** 33.13%
- **Max drawdown:** ₹-6,591.31
- **Days hitting ₹5k cap:** 0
- **Stagnation exits:** 0

## Acceptance gates

- PASS — ≥ 40 trades
- FAIL — Expectancy ≥ +0.15R
- FAIL — MFE capture ≥ 55%
- PASS — Max DD < 3 daily caps

## Exit counts

- index_structure_stop: 30
- mfe_ratchet_stop: 47

## Top rejection counts

- breadth/index disagreement: 1207
- pullback_not_confirmed: 1061
- lunch_chop: 654
- top mover VWAP/rel-vol confirmation missing: 386
- chop_regime: 346
- risk_over_cap: 45
- burned_level: 24
- weighted VWAP side 51.65% < 60%: 16
- weighted VWAP side 58.16% < 60%: 12
- weighted VWAP side 50.44% < 60%: 11
- weighted VWAP side 0.00% < 60%: 11
- weighted VWAP side 30.14% < 60%: 11

Trade CSV: `/opt/data/finance-db/reports/banknifty_pullback_v2_proxy_trades_20260612_061414.csv`
