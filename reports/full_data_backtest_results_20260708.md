# Full-Data Backtest Results — All Strategies (2020–2026)

**Run date:** 2026-07-08
**Run time:** All 7 strategies completed in parallel (~3 min)
**Index data:** DhanHQ option spot (Aug 2020–Jul 2025) + FYERS (Feb–Jul 2026) merged
**Report files:** See `reports/` for individual backtest markdown reports and CSVs.

---

## Data Foundation

164,972 index candles reconstructed from DhanHQ option `spot` values:
- **NIFTY50-INDEX** (Aug 2020 – Jul 2025): 73,715 candles → merged to 99,715 with FYERS
- **NIFTYBANK-INDEX** (Aug 2021 – Jul 2025): 56,529 candles → merged to 80,907 with FYERS
- ~7-month gap (Jul 2025–Feb 2026) between Dhan expiry data and FYERS 100-day window

Script: `scripts/rebuild_index_candles_from_dhan.py`

---

## Desk 1: BankNifty Options

### BN Directional Debit Spread (DDS) — Unfiltered (CE+PE)
| Metric | Value |
|---|---|
| Period | 2021-08-04 → 2026-07-08 |
| Trades | 861 |
| Wins / Losses | 366 / 495 |
| Win Rate | 42.51% |
| Total P&L | **₹+72,289.44** ✅ |
| Avg P&L/trade | ₹+84 |

**Note:** Both CE and PE signals included (unfiltered). PE-only mode is only available in paper/live.

### BN Pullback v2
| Metric | Value |
|---|---|
| Period | 2021-08-04 → 2026-07-08 |
| Trading days scanned | 1,079 |
| Trades | 87 |
| Win Rate | 39.08% |
| Total P&L | ₹+7,363.80 |
| Expectancy | +0.07R |
| Avg Win / Avg Loss | 0.98R / -0.51R |
| Max Drawdown | ₹-5,863.70 |
| Profit Factor | 0.78 |

**Acceptance gates:** Expectancy ≥ 0.15R ❌, MFE capture ≥ 55% ❌, Trades ≥ 40 ✅, Max DD < 3 caps ✅

**Note:** 0 trades in 2020–2025. Strategy requires NIFTYBANK-INDEX constituents (HDFCBANK, SBIN, ICICIBC, etc.) for breadth/confirmation filters. Only FYERS-era (Feb 2026+) constituent candles exist. DhanHQ equity candle data needed for pre-2026 backtest.

Report: `reports/banknifty_pullback_v2_proxy_backtest_20260708_173839.md`

### BN Iron Condor
| Metric | Value |
|---|---|
| Period | 2021-08-04 → 2026-07-08 |
| Days scanned | 1,286 |
| Signals | **0** ❌ |

**Issue:** Timezone bug (UTC filter vs IST market hours). Need fix in `scripts/iron_condor_strategy.py`.

---

## Desk 2: NSE Strategy Pack

### Overall (All 4 Strategies) — 2020-08-03 to 2026-07-08
| Strategy | Trades | Wins/Losses | P&L | Win Rate | Profit Factor | Avg R |
|---|---|---|---|---|---|---|
| CPR Trend-Day Debit Spread | 364 | 133/231 | **-₹39,193.09** | 36.5% | 0.83 | -0.08 |
| Expiry Tuesday Nifty Directional | 201 | 100/101 | **-₹25,072.64** | 49.8% | 0.83 | -0.08 |
| Single-Stock Momentum + Index Confirm | 35 | 13/22 | **+₹5,947.17** ✅ | 37.1% | 1.28 | +0.12 |
| Nifty VWAP Mean Reversion | 11 | 4/7 | **-₹2,363.31** | 36.4% | 0.73 | -0.15 |
| **Total** | **611** | 250/361 | **-₹60,681.87** | 40.9% | — | — |

Report: `reports/nse_intraday_options_strategy_pack_proxy_backtest_20260708_173803.md`

---

## Desk 3: Equity Strategies

### Nifty VWAP Reversion v2
| Metric | Value |
|---|---|
| Period | 2020-08-03 → 2026-07-08 |
| Trades | 426 |
| Wins / Losses | 203 / 223 |
| Win Rate | 47.65% |
| Total P&L | **₹-104,409.82** |
| Avg P&L/trade | ₹-245 |

**Note:** Breakeven WR would be ~67.6% (given 1:2 R:R); actual is 47.7%. Negative edge consistent across pre-2025 and post-2025 eras.

### Nifty Iron Condor
| Metric | Value |
|---|---|
| Period | 2020-08-03 → 2026-07-08 |
| Signals | **0** ❌ |

**Issue:** Proxy premium model produces max ₹109 vs ₹300 minimum credit threshold. Needs calibration of `index_option_premium` or `min_credit` reduction.

### FTS Swing (Equity Universe)
| Metric | Value |
|---|---|
| Period | 2025-06-05 → 2026-06-05 |
| Universe | 200 symbols (active.csv) |
| Trades | 27 |
| Wins / Losses | 16 / 11 |
| Win Rate | 59.26% |
| Net P&L | **₹+338.89** |
| Profit Factor | 2.34 |
| Avg Win / Avg Loss | ₹+36.98 / ₹-22.99 |
| Max Drawdown | ₹87.52 |
| Ending Equity | ₹5,338.89 |

**Note:** Fundamental/sentiment evidence uses neutral placeholders — not yet populated from real F+S tables. Encouraging given technical-only signals.

Report: `reports/fts_swing_v1_backtest_20260708_120832.md`

---

## Summary Table

| Strategy | Period | Trades | P&L | Win Rate | Score |
|---|---|---|---|---|---|
| BN DDS | 2021–2026 | 861 | **+₹72,289** | 42.5% | ✅ Profitable |
| BN Pullback v2 | 2021–2026* | 87 | +₹7,364 | 39.1% | ⚠️ Mixed (needs constituents) |
| NSE Strategy Pack | 2020–2026 | 611 | **-₹60,682** | 40.9% | ❌ Negative |
| Nifty VWAP | 2020–2026 | 426 | **-₹104,410** | 47.7% | ❌ Deeply negative |
| FTS Swing | 2025–2026 | 27 | +₹339 | 59.3% | ✅ Encouraging (tech-only) |
| BN Iron Condor | 2021–2026 | 0 | ₹0 | — | 🔧 Bug (timezone) |
| Nifty Iron Condor | 2020–2026 | 0 | ₹0 | — | 🔧 Bug (premium model) |

*\*BN Pullback only trades in FYERS era (Feb 2026+) due to constituent candle gap*

---

## Action Items

1. **DDS PE-only CLI flag** — add `--pe-only` to scan-range mode for dedicated PE-only full backtest
2. **Single-Stock Momentum** standout performer — consider scaling allocation or tighter rules
3. **Iron Condors** — fix timezone bug (BN) + proxy premium model (Nifty)
4. **BN Pullback v2** — needs DhanHQ constituent stock candles for 2021–2026 era
5. **FTS Swing** — populate F+S evidence tables, then rerun with real scores
6. **Nifty VWAP + CPR Trend-Day + Expiry Tuesday** — all deeply negative across full data; consider disabling