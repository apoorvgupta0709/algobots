# Full-Data Backtest Results — All Strategies (2020–2026)

**Run date:** 2026-07-08  
**Index data:** DhanHQ option spot (Aug 2020–Jul 2025) + FYERS (Feb–Jul 2026) merged  
**Report files:** See `reports/` for individual backtest markdown reports and CSVs.

---

## Data Extension

164,972 index candles reconstructed from DhanHQ option `spot` values:
- **NIFTY50-INDEX** (Aug 2020 – Jul 2025): 73,715 candles → merged to 99,703 with FYERS
- **NIFTYBANK-INDEX** (Aug 2021 – Jul 2025): 56,529 candles → merged to 80,895 with FYERS
- ~7-month gap (Jul 2025–Feb 2026) between Dhan expiry data and FYERS 100-day window

Script: `scripts/rebuild_index_candles_from_dhan.py`

---

## Desk 1: BankNifty Options

### BN Pullback v2
| Metric | Value |
|---|---|
| Trades | 87 |
| Win Rate | 39.08% |
| Total P&L | ₹+3,476.35 |
| Profit Factor | 0.78 |
| Max DD | ₹-6,389.19 |
| Days scanned | 1,079 |
| Trading days (2026 only) | 106 |
| Period active | Feb–Jul 2026 only |

**Note:** 0 trades in 2020–2025. Strategy requires NIFTYBANK-INDEX constituents (HDFCBANK, SBIN, ICICIBC, etc.) for breadth/confirmation filters. Only FYERS-era (Feb 2026+) constituent candles exist. To backtest pre-2026, DhanHQ equity candle data must be ingested.

Report: `reports/banknifty_pullback_v2_proxy_backtest_20260708_144514.md`

### BN Directional Debit Spread (DDS) — V1
| Metric | Value |
|---|---|
| Trades | 861 |
| Win Rate | 42.5% |
| Total P&L | ₹+72,289.44 |
| Avg P&L/trade | ₹+84 |
| Period | 2020-08-01 → 2026-07-08 |

**Note:** Run with both CE+PE signals (unfiltered). PE-only mode is only wired for paper/live. The unfiltered run shows a slight positive edge (+₹84/trade) but 42.5% win rate with 2R targets.

### BN Iron Condor
**0 signals** — Timezone bug (UTC filter vs IST market hours). Needs fix in `scripts/iron_condor_strategy.py`.

---

## Desk 2: NSE Strategy Pack

### Overall (All 4 Strategies)
| Strategy | Trades | P&L | Win Rate | Profit Factor |
|---|---|---|---|---|
| CPR Trend-Day Debit Spread | 364 | **-₹39,193.09** | 36.5% | 0.83 |
| Expiry Tuesday Nifty Directional | 201 | **-₹25,072.64** | 49.8% | 0.83 |
| Single-Stock Momentum + Index Confirm | 35 | **+₹5,947.17** ✅ | 37.1% | 1.28 |
| Nifty VWAP Mean Reversion | 11 | **-₹2,363.31** | 36.4% | 0.73 |
| **Total** | **611** | **-₹60,681.87** | 40.9% | — |

Report: `reports/nse_intraday_options_strategy_pack_proxy_backtest_20260708_145129.md`

---

## Desk 3: Equity Strategies

### Nifty VWAP Reversion v2
| Metric | Value |
|---|---|
| Trades | 426 |
| Win Rate | 47.65% |
| Total P&L | **₹-104,409.82** |
| Avg P&L/trade | ₹-245 |
| Period | 2020-08-01 → 2026-07-08 |

**Note:** Breakeven WR would be ~67.6% (given 1:2 R:R); actual is 47.7%. Negative edge in both pre-2025 and post-2025 eras.

### Nifty Iron Condor
**0 signals** — Proxy premium model produces max ₹109 vs ₹300 minimum credit threshold. Needs calibration of `index_option_premium` formula or reduction of `min_credit`.

### FTS Swing (Equity Universe)
| Metric | 10-Symbol Run | Full Run (~200 symbols) |
|---|---|---|
| Trades | 13 | 27 |
| Win Rate | 69.2% | 59.3% |
| Total P&L | ₹+352 | ₹+339 |
| Return on capital | +7.0% | +6.8% |
| Profit Factor | 6.90 | 2.34 |
| Period | 2025-06-05 → 2026-06-05 | |

**Note:** Fundamental/sentiment evidence uses neutral placeholders — not yet wired to real F+S tables. All scores in `paper_setup` range (65–80); no `high_conviction` (>80). Encouraging given 2025 data only.

Reports: `reports/fts_swing_v1_backtest_20260708_092444.md`, `reports/fts_swing_v1_backtest_20260708_092512.md`

---

## Action Items

1. **DDS PE-only CLI flag** — add `--pe-only` to scan-range mode for full 2020-2026 PE-only backtest
2. **Single-Stock Momentum** standout performer — consider scaling allocation or tighter rules
3. **Iron Condors** — fix timezone bug (BN) + proxy premium model (Nifty)
4. **BN Pullback v2** — needs DhanHQ constituent stock candles for pre-2026 era
5. **FTS Swing** — populate F+S evidence tables, then rerun
6. **Nifty VWAP & CPR Trend-Day** — both deeply negative, consider disabling
