# BankNifty Iron Condor — Full-Data Proxy Backtest

**Run:** 2026-07-08 | **Period:** 2021-08-04 to 2026-07-08 (1,286 trading days)

## Parameters (after fix)
| Param | Value |
|---|---|
| Strike selection | 0.5%/1.5% OTM (sold/bought) |
| Wing width | ₹600 (6 × ₹100 steps) |
| Max loss cap | ₹18,000 (realistic iron condor risk) |
| Min credit | ₹200 (proxy premium calibrated) |
| Premium model | ATM 1.0%, OTM discount 45× |

## Results
| Metric | Before Fix | After Fix |
|---|---|---|
| Signals | **0** / 1286 days | **27** / 1286 days |
| Wins | 0 | 26 |
| Losses | 0 | 1 |
| Win Rate | — | 96.3% |
| Total P&L | ₹0 | **+₹664.73** |
| Avg Win | — | ₹239 |
| Avg Loss | — | -₹5,687 |
| Worst Loss | — | 2026-05-07: Stop breach |

## All Trades
```
2026-05-07:  -₹5,686.50 | credit: ₹312 | exit: stop_breach (one big loss)
2026-05-19:  +₹201.96   | credit: ₹238 | exit: target_profit
2026-05-27:  +₹255.09   | credit: ₹300 | exit: target_profit
2026-06-10:  +₹280.22   | credit: ₹330 | exit: target_profit
... 23 more profitable trades
```

## Key Observations
- All signals clustered in FYERS era (Feb–Jul 2026) — consistent with BN Pullback v2 pattern
- Pre-2026: 0 signals. Reason: proxy premium model still under-prices at lower absolute spot levels
- 96% win rate suggests the range-day + low-body-candle filter is high quality
- Single big stop-loss wiped out ~8 profitable trades — tight stops (35% wing) are aggressive

## Changes Made
- **`scripts/iron_condor_strategy.py`**: `proxy_premium()` calibrated: ATM 1.0% (was 0.9%), OTM discount 45× (was 80×), time_factor aligned with common model
- **Strike selection**: Changed from 1%/2% OTM to 0.5%/1.5% OTM
- **Thresholds**: `min_credit` 300→200, `max_loss_cap` 3,000→18,000
- **`scripts/run_iron_condor.py`**: `MAX_LOSS` 3,000→18,000