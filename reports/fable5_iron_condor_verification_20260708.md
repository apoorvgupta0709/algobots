# Fable 5 Verification Report — Iron Condor Fix

**Model:** Claude Fable 5 via Claude Code CLI
**Verification date:** 2026-07-08
**Mode:** Read-only static inspection + hand-computed arithmetic

## Verdict
> The fix fully explains the 0-signal behavior: the old ₹3,000 cap was mathematically unsatisfiable for BankNifty (any ≥1-step wing = ₹3,000 gross risk × 30 lot, and actual ~600-pt wings implied ~₹17.4k risk → every candidate rejected at the risk gate), while on Nifty the old ₹300 min_credit sat above the ~₹108 achievable proxy credit → every candidate rejected at the credit gate; both gates now clear with realistic margins.

## Consistency Check
| Item | Expected | Found | Status |
|---|---|---|---|
| BN ATM premium | 1.0% of spot | `spot * Decimal("0.010")` — line 101 | ✅ |
| OTM discount denominator | 45 | `1 / (1 + moneyness * 45)` — line 117 | ✅ |
| time_factor tiers | ≤1→0.70, ≤3→0.90, ≤7→1.00, >7→1.10 | Lines 107–114 match `index_option_premium` in `proxy_backtest_common.py:154-161` | ✅ |
| Strike selection | 0.5% / 1.5% OTM | `0.995/0.985/1.005/1.015 × entry_spot` — lines 238-241 | ✅ |
| BN min_credit | 200 | `Decimal("200")` — default at line 171 | ✅ |
| BN max_loss_cap | 18000 | `Decimal("18000")` — default at line 170 | ✅ |
| Nifty min_credit | 100 | `Decimal("100")` — `nifty_iron_condor_strategy.py:55` | ✅ |
| Nifty max_loss_cap | 7500 | `Decimal("7500")` — `nifty_iron_condor_strategy.py:54` | ✅ |
| `run_iron_condor.py` MAX_LOSS | 18000 | `Decimal("18000")` — line 44 | ✅ |

## Stale Docstrings (fixed post-verification)
- Module-level docstring: "Max loss: ₹3,000" → ₹18,000 ✅
- `evaluate_bn_iron_condor` docstring: strike selection + thresholds updated ✅
- Wrapper `evaluate_iron_condor` default `max_loss_cap=3000` → 18000, `min_credit` now a proper parameter ✅