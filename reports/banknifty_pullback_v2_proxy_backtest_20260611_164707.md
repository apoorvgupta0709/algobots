> **⚠️ SUPERSEDED (2026-07-08) — DO NOT USE THESE NUMBERS.**
> This report was generated BEFORE the 2026-06-11/12 trading-logic bug fixes
> (inverted relative-strength check, dead-code ORB filter, breakeven/ratchet
> timing, and the missing cost line). Its headline figures (~87% win rate,
> ~1.2R expectancy, ~₹101k P&L) are inflated and do not reflect the current
> code. Current reference run: `banknifty_pullback_v2_proxy_backtest_20260612_061414.md`
> (77 trades, 61% win rate, 0.13R expectancy). See `reports/SUPERSEDED.md`.

# BankNifty Pullback Continuation v2 — Proxy Backtest

Research-only; no orders placed.

## Data caveat

Stored 1-min/5-min BankNifty index and constituent candles were used. Historical expired BankNifty option-chain candles are not available in the current FYERS master, so the option leg is simulated with the configured index-to-option beta risk model. Treat this as signal/risk validation, not final option-candle acceptance.

## Summary
- **Window:** 2026-02-01 to 2026-06-11
- **Trading days:** 88
- **No-trade days:** 28
- **Trades:** 115
- **Win rate:** 86.96%
- **Total P&L:** ₹101,164.69
- **Expectancy:** 1.22R
- **Avg win:** 1.56R
- **Avg loss:** -1.00R
- **Avg MFE capture:** 48.87%
- **Max drawdown:** ₹-5,543.57
- **Days hitting ₹5k cap:** 0
- **Stagnation exits:** 0

## Acceptance gates

- PASS — ≥ 40 trades
- PASS — Expectancy ≥ +0.15R
- FAIL — MFE capture ≥ 55%
- PASS — Max DD < 3 daily caps

## Exit counts

- force_intraday_exit: 1
- index_structure_stop: 15
- mfe_ratchet_stop: 99

## Top rejection counts

- breadth/index disagreement: 1215
- pullback_not_confirmed: 1016
- lunch_chop: 681
- top mover VWAP/rel-vol confirmation missing: 401
- chop_regime: 353
- weighted VWAP side 51.65% < 60%: 16
- risk_over_cap: 14
- burned_level: 13
- weighted VWAP side 58.16% < 60%: 12
- weighted VWAP side 50.44% < 60%: 11
- weighted VWAP side 0.00% < 60%: 11
- weighted VWAP side 30.14% < 60%: 11

Trade CSV: `/opt/data/finance-db/reports/banknifty_pullback_v2_proxy_trades_20260611_164707.csv`
