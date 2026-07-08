# BankNifty Pullback Continuation v2 — Proxy Backtest

Research-only; no orders placed.

## Exit parameters used

- breakeven_at_r: 0.5
- ratchet_start_r: 1.0
- ratchet_giveback_pct: 30
- ratchet_giveback_min_inr: 300
- round_trip_cost_inr: 100
- cost_aware_breakeven: True

## Data caveat

Stored 1-min/5-min BankNifty index and constituent candles were used. Historical expired BankNifty option-chain candles are not available in the current FYERS master, so the option leg is simulated with the configured index-to-option beta risk model. Treat this as signal/risk validation, not final option-candle acceptance.

Cost model: ₹100 round-trip per trade (brokerage + slippage), subtracted from every trade; breakeven/ratchet lock is cost-aware unless --legacy-gross-breakeven is set.

## Summary
- **Window:** 2021-08-04 to 2026-07-08
- **Trading days:** 1079
- **No-trade days:** 1023
- **Trades:** 87
- **Win rate:** 39.08%
- **Total P&L:** ₹7,363.80
- **Expectancy:** 0.07R
- **Avg win:** 0.98R
- **Avg loss:** -0.51R
- **Avg MFE capture:** 25.22%
- **Max drawdown:** ₹-5,863.70
- **Days hitting ₹5k cap:** 0
- **Stagnation exits:** 0

## Acceptance gates

- PASS — ≥ 40 trades
- FAIL — Expectancy ≥ +0.15R
- FAIL — MFE capture ≥ 55%
- PASS — Max DD < 3 daily caps

## Exit counts

- index_structure_stop: 25
- mfe_ratchet_stop: 62

## Top rejection counts

- breadth/index disagreement: 1310
- pullback_not_confirmed: 1099
- lunch_chop: 710
- top mover VWAP/rel-vol confirmation missing: 424
- chop_regime: 368
- risk_over_cap: 45
- burned_level: 21
- weighted VWAP side 51.65% < 60%: 16
- weighted VWAP side 58.16% < 60%: 12
- weighted VWAP side 50.44% < 60%: 11
- weighted VWAP side 0.00% < 60%: 11
- weighted VWAP side 30.14% < 60%: 11

Trade CSV: `/opt/data/finance-db/reports/banknifty_pullback_v2_proxy_trades_20260708_173839.csv`
