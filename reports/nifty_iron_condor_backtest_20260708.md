# Nifty Iron Condor — Full-Data Proxy Backtest

**Run:** 2026-07-08 | **Period:** 2020-08-03 to 2026-07-08

## Parameters (after fix)
| Param | Value |
|---|---|
| Strike selection | 1 step OTM, 3-step wings (₹150) |
| Max loss cap | ₹7,500 |
| Min credit | ₹100 (lowered from ₹300) |

## Results
| Metric | Before Fix | After Fix |
|---|---|---|
| Signals | **0** | **359** |
| Wins | 0 | 229 |
| Losses | 0 | 130 |
| Win Rate | — | 63.8% |
| Total P&L | ₹0 | **-₹184,279** |
| Avg Win | — | ₹75 |
| Avg Loss | — | -₹1,418 |

## Root Cause
Nifty ₹50 strike steps × 3-step wings = ₹150 wide. With 25 lot, gross risk = ₹3,750. The `index_option_premium()` model produces net credit of ~₹108 per trade, well below the previous ₹300 threshold.

## Fix Applied
- `min_credit` lowered from ₹300 to ₹100 (matches realistic Nifty IC credit at current spot levels)
- `max_loss_cap` raised from ₹3,000 to ₹7,500 (matches 150pt × 25 lot risk)

## Issue Remaining
Nifty IC has poor risk/reward: ₹75 avg win vs ₹1,418 avg loss. The small credit means 1 stop-loss wipes out ~19 profitable days. Strategy needs tighter stops, narrower wings, or higher credit threshold in paper/live mode.