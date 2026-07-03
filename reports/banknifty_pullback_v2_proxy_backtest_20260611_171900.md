# BankNifty Pullback Continuation v2 — Proxy Backtest

Research-only; no orders placed.

## Data caveat

Stored 1-min/5-min BankNifty index and constituent candles were used. Historical expired BankNifty option-chain candles are not available in the current FYERS master, so the option leg is simulated with the configured index-to-option beta risk model. Treat this as signal/risk validation, not final option-candle acceptance.

## Summary
- **Window:** 2026-02-01 to 2026-06-11
- **Trading days:** 88
- **No-trade days:** 28
- **Trades:** 116
- **Win rate:** 85.34%
- **Total P&L:** ₹100,870.76
- **Expectancy:** 1.20R
- **Avg win:** 1.58R
- **Avg loss:** -1.00R
- **Avg MFE capture:** 56.49%
- **Max drawdown:** ₹-5,543.57
- **Days hitting ₹5k cap:** 0
- **Stagnation exits:** 0

## Acceptance gates

- PASS — ≥ 40 trades
- PASS — Expectancy ≥ +0.15R
- PASS — MFE capture ≥ 55%
- PASS — Max DD < 3 daily caps

## Exit counts

- force_intraday_exit: 1
- index_structure_stop: 17
- mfe_ratchet_stop: 98

## Top rejection counts

- breadth/index disagreement: 1206
- pullback_not_confirmed: 1033
- lunch_chop: 691
- top mover VWAP/rel-vol confirmation missing: 404
- chop_regime: 356
- weighted VWAP side 51.65% < 60%: 16
- risk_over_cap: 16
- burned_level: 13
- weighted VWAP side 58.16% < 60%: 12
- weighted VWAP side 50.44% < 60%: 11
- weighted VWAP side 0.00% < 60%: 11
- weighted VWAP side 30.14% < 60%: 11

Trade CSV: `/opt/data/finance-db/reports/banknifty_pullback_v2_proxy_trades_20260611_171900.csv`
