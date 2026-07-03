# Indian Markets Trading Strategy Compendium — Registry Mapping

> Paper/research only. No live orders are placed anywhere in this system, and no
> broker/FYERS order-placement code exists. This document reconciles the strategy
> registry (`config/strategy_universe_india.json`) to the
> `Indian_Markets_Trading_Strategy_Compendium.pdf`, which contains **49 strategy
> sections across 6 chapters** (Chapters 1-2 are framing/risk; the mapped strategy
> chapters are 3 Investing, 4 Swing, 5 Intraday, 6 Options, 7 Futures).

Every PDF strategy section is mapped to a registry strategy id below. `Status`
is `new` (added in this reconciliation), `enriched` (an existing entry rewritten
to match the compendium), or `existing` (already fully covered, left unchanged).
`Executable` reflects the registry: executable strategies emit paper trades and
carry a rupee risk block; scorecard-only strategies never trade (short-premium,
undefined-risk, single-stock-option, futures, and data-gated studies).

## Section-by-section coverage

| Section | Compendium Title | Registry Strategy ID | Status | Executable | Notes |
| --- | --- | --- | --- | --- | --- |
| 3.1 | Passive Index Core (Nifty 50 / Nifty Next 50 / total market) | `passive_index_core_scorecard` | new | no (scorecard) | NEW multi-vehicle passive-core portfolio scorecard (Nifty 50 + Next 50 + Nifty 500, direct plans <0.25% TER, annual rebalance, ETF liquidity/iNAV note). The single-ETF executable analogue remains sip_baseline_buy_and_hold. |
| 3.2 | SIP / Rupee-Cost Averaging | `sip_step_up_value_averaging` | enriched | yes | ENRICHED existing executable entry with doc-specific salary-linked step-up, rupee-cost averaging sized to savings rate, the optional 10%+ correction lump-sum top-up rule, and the 12.5% LTCG / Rs 1.25 lakh churn note plus matching data_requirements. sip_baseline_buy_and_hold stays the flat benchmark, unchanged. |
| 3.3 | Asset Allocation and Rebalancing | `strategic_asset_allocation_rebalance_scorecard` | new | no (scorecard) | NEW doc-faithful 60/30/10 equity/debt/gold strategic-allocation scorecard with calendar (yearly) AND 5pp tolerance-band rebalance and the balanced-advantage-fund tax note. Existing risk_parity_allocation, niftybees_goldbees_allocation_scorecard and rebalance_bands_tax_harvest_overlay_scorecard remain complementary and unchanged. |
| 3.4 | Value Investing (Graham-style screens) | `graham_value_screen_scorecard` | new | no (scorecard) | NEW Graham deep-value scorecard (P/E<12-14, P/B<1.5, D/E<0.5, positive OCF, dividend record, margin of safety, 15-25 equal-weight, governance-FIRST filter). Distinct from the cross-sectional value_factor_composite_scorecard; cross-referenced in rationale. |
| 3.5 | Quality / Coffee-Can Compounding | `quality_coffee_can_scorecard` | new | no (scorecard) | NEW coffee-can quality scorecard (ROCE/ROE>18-20% for 10y, revenue growth>10%, negligible debt, decade-long hold, act only on business deterioration). Distinct from blended quality_value_growth_composite; cross-referenced. |
| 3.6 | GARP - Growth at a Reasonable Price | `garp_reasonable_growth_scorecard` | new | no (scorecard) | NEW GARP scorecard (EPS growth 15%+, PEG<1-1.2, improving margins, low leverage; enter on consolidations; exit PEG>~2 or two quarters of deceleration; cash-flow-conversion cross-check). |
| 3.7 | Dividend-Yield Investing | `dividend_yield_income_scorecard` | new | no (scorecard) | NEW dividend-yield income scorecard (yield>3-4%, payout<60%, stable earnings, net cash/low debt; yield-trap and PSU-payout caveats; slab-tax-vs-buyback note). |
| 3.8 | Momentum Factor (12-month formation) | `momentum_factor_composite_scorecard` | enriched | no (scorecard) | ENRICHED existing 12-1 momentum scorecard to make the Nifty 200 universe, top-20-30 hold, and passive Nifty 200 Momentum 30 index-fund route (single-tax-event edge) explicit, plus the regime-crash reference to momentum_crash_guard_overlay_scorecard. |
| 3.9 | Low-Volatility Factor | `low_volatility_portfolio_scorecard` | existing | no (scorecard) | Already fully covers 3.9: 30 lowest-volatility names / Nifty 100 Low Vol 30 route, semi-annual rebalance, pairs with momentum, crowding/laggard risk. No change needed; not added to upserts. |
| 3.10 | Magic Formula (Greenblatt, India edition) | `magic_formula_india_scorecard` | new | no (scorecard) | NEW Magic Formula India scorecard (rank EBIT/EV earnings yield + ROC, sum ranks, top 20-30 equal-weight, annual, >Rs 500cr, exclude financials, add governance/pledging filter). |
| 3.11 | CANSLIM (O'Neil growth-momentum hybrid) | `canslim_growth_momentum_scorecard` | new | no (scorecard) | NEW CANSLIM scorecard; data_requirements enumerate each C/A/N/S/L/I/M input, M maps to Nifty>50&200DMA, hard 7-8% stop. Positional timeframe; scorecard-only due to fundamentals/institutional/event data + multi-name accounting. |
| 3.12 | Special Situations | `special_situations_event_scorecard` | new | no (scorecard) | NEW event-driven special-situations scorecard (buyback tender arb, demergers, open offers, delisting, promoter buying, index inclusion; SEBI Rs 2 lakh small-shareholder note; low beta correlation). Event-data dependent, non-executable. |
| 3.13 | Sector Rotation | `sector_rotation_positional` | enriched | no (scorecard) | ENRICHED existing entry to add the doc's macro cycle map (credit growth, PMI, rates, capacity utilisation), the simpler 6-month relative-strength proxy, and policy (budgets/PLI/RBI) as a first-class cycle input. Timeframe kept positional. |
| 4.1 | 52-Week-High / Darvas Box Breakout | `darvas_box_52w_high_equity` | existing | yes | Already covers 3-8 week box (ceiling/floor), close above box high on 1.5-2x volume, stop below box low / 20-DMA, box-trailing ratchet, 52w-high + positive RS vs Nifty. swing_range_breakout_equity is a companion base-breakout. No change needed. |
| 4.2 | Pullback-to-Trend (buy dips in uptrends) | `swing_pullback_rising_ma_equity` | enriched | yes | Rewritten with Brooks methodology: first touch of 20-EMA / 38-50% retrace, first AND second entry, explicit reversal/reclaim trigger candle, stop just below the pullback low, measured-move target, breakeven at +0.8R and ratchet trail (~60% MFE). No buy-and-hold-N-bars. |
| 4.3 | Moving-Average Crossover System | `ma_crossover_swing_equity` | new | yes | New mechanical long: 20/50-EMA golden cross (swing) or 50/200-DMA (positional); exit on reverse cross or trail under the slower MA; 40-50% win rate carried by 2.5R+ winners; low frequency keeps it cost-viable. Ratchet trail added. |
| 4.4 | Supertrend / ADX Trend Following | `supertrend_adx_trend_equity` | new | yes | New long: close above Supertrend(10,3) with ADX>20-25; Supertrend line is the trailing stop; stand aside if ADX<20; ATR stop widens so size is recomputed from the CURRENT stop distance (reduce size, never widen the stop). |
| 4.5 | RSI Mean Reversion (within uptrends) | `rsi2_uptrend_reversion_equity` | existing | yes | Already covers the rising-200-DMA universe filter, short-period RSI deep-oversold buy, RSI-recovery / short-MA exit, ATR/percentage hard stop and a few-session time stop. large_cap_mean_reversion_swing is a companion. No change needed. |
| 4.6 | Bollinger Bands - Squeeze and Reversion | `bollinger_squeeze_breakout_equity`, `bollinger_band_reversion_equity` | new | yes | Two new entries. Squeeze: band-width 6-month low then trade the expansion break direction (long), opposite band as stop, measured move. Reversion: in flat trends fade lower-band closes back to the 20-day mean. Both emphasise classify-the-regime-first (modes are mutually exclusive). |
| 4.7 | Classical Chart Patterns | `classical_chart_pattern_scorecard` | new | no (scorecard) | New scorecard (executable:false, structure none, risk null). Continuation patterns (flags/pennants/cup-and-handle) scored on breakout+volume with measured-move targets and pattern-internal stops; H&S/double-top treated as exit signals; invalidation predefined; India note that positional shorts need futures/puts. Manual-review / backtest only. |
| 4.8 | Relative-Strength Leaders | `relative_strength_leader_equity` | existing | yes | Already covers ranking by 3/6-month return vs Nifty, buying top-decile leaders on pullbacks/breakouts only while Nifty is above its 50-DMA, and dropping holdings whose RS rank decays below median. sector_relative_strength_equity covers the sector cousin. No change needed. |
| 4.9 | Earnings and Event Swing | `post_earnings_drift_scorecard_equity` | existing | no (scorecard) | Covered by the post-earnings-drift event-study scorecard, which measures the earnings-gap reaction and multi-day drift persistence (1/5/20 sessions) that the 'no positions into the print / gap-and-hold / drift 3-15 days' playbook exploits. Remains a non-executable study; no change needed. |
| 4.10 | Pair Trading / Statistical Arbitrage | `pairs_market_neutral_equity` | enriched | no (scorecard) | Enriched AND downgraded executable->scorecard. Added cointegration, 60-120d spread z-score, enter \|z\|>2 (long cheap / short rich leg via single-stock futures), exit near z=0, hard stop \|z\|>3 or structural break, several-lakh futures margin ticket. Now executable:false, direction market_neutral, structure none, risk null (short leg lives in futures - not modelled live). |
| 5.1 | Opening Range Breakout (ORB) | `orb_retest_equity` | existing | yes | Covered by orb_retest_equity (09:15-09:45 range, break then retest-hold reclaim entry, stop at range/retest low, +1R partial then trail) and orb_initiation_equity for the direct-break variant. No change needed. |
| 5.2 | VWAP Mean Reversion | `vwap_mean_reversion_equity` | existing | yes | Covered by vwap_mean_reversion_equity (range-day only, >=2 sigma stretch below VWAP + reversal candle, target the VWAP touch, at most twice a day) and vwap_band_reversion_intraday_equity. No change needed. |
| 5.3 | VWAP Trend Rides | `vwap_trend_equity` | enriched | yes | Enriched with explicit first-AND-second-kiss-of-VWAP entry, trend-day classification (one-sided open, holds above VWAP first hour, narrow CPR), skip the third-plus touch (usually breaks), stop just below VWAP, ratchet trail. |
| 5.4 | CPR (Central Pivot Range) Framework | `cpr_trend_day_equity` | existing | yes | Covered by cpr_trend_day_equity: narrow-CPR trend-day bias, break above the pivot/TC with index above its own pivot, stop back inside CPR, abort on repeated pivot rejections. The wide-CPR range-fade side is covered by the new range_fade_support_resistance_equity (5.8). No change needed. |
| 5.5 | Gap Trading (gap-and-go / gap-fill) | `gap_up_continuation_equity`, `gap_fill_reclaim_equity` | enriched | yes | Both variants enriched. gap-and-go (gap_up_continuation_equity): explicit ~0.4-0.5% gap threshold, holds above first-15-min low on volume, consolidation-break entry, GIFT-Nifty cue. gap-fill (gap_fill_reclaim_equity): re-scoped to news-less drift gap that loses the open, faded to prior close on opening-range + VWAP reclaim, GIFT-Nifty context; gap_down_fade_equity remains a companion fade. |
| 5.6 | Pullback Continuation (intraday) | `intraday_pullback_continuation_equity` | new | yes | New executable long with Brooks retest methodology: after any confirmed intraday breakout, retest the broken level (or 20-EMA on 5-min), enter on the reclaim candle, stop just beyond the retest low (tight stop roughly doubles R), ratchet-trail the runner, skip the 11:30-13:15 chop. Broader than the opening-range-specific orb_retest_equity. |
| 5.7 | Momentum Burst / News Scalps (with index confirmation) | `single_stock_momentum_index_confirm` | existing | yes | Covered by single_stock_momentum_index_confirm (stock-in-play break only while the index agrees, stop under the trigger, scale out fast) plus high_rvol_momentum_continuation_equity (3-5x relative-volume bursts) and volume_shock_equity. No change needed. |
| 5.8 | Range Fade at Support/Resistance | `range_fade_support_resistance_equity` | new | yes | New executable long: confirmed range (two touches each side + wide CPR), fade the third-plus touch of support on a rejection candle, stop just beyond the extreme, target mid/opposite edge, abandon on a volume break, and skip index targets thinner than the ~0.25% friction floor. Long-only (fade support only). |
| 6.1 | Long Call / Long Put | `index_long_call_put_directional` | new | yes | New INDEX-only defined-risk executable: single near-ATM CE/PE on momentum trigger, premium-capped loss. Single-stock long options excluded (spreads too wide). |
| 6.2 | Debit Vertical Spreads (bull call / bear put) | `banknifty_debit_spread_directional` | existing | yes | Already covered by banknifty/nifty/finnifty/orb/cpr/second-entry/flag/gap debit-spread executables. No change. |
| 6.3 | Credit Vertical Spreads (bull put / bear call) | `credit_spread_directional_scorecard` | existing | no (scorecard) | Covered by credit_spread_directional_scorecard; verified structure=credit_spread, option_selling=false-net-short, executable=false, risk=null. Stays non-executable. |
| 6.4 | Short Strangle | `short_strangle_scorecard` | existing | no (scorecard) | Covered by short_strangle_scorecard (structure=strangle, option_selling=true, executable=false). No change. |
| 6.5 | Short Straddle (incl. 9:20 variant) | `short_straddle_scorecard` | enriched | no (scorecard) | Enriched with the intraday 9:20 variant (sell both ATM legs post-auction, fixed 25-30% per-leg SL, square 3:00-3:15) and SL-M slippage/+2% ELM warning. Stays scorecard, option_selling=true. |
| 6.6 | Iron Condor | `iron_condor_scorecard` | existing | no (scorecard) | Covered by iron_condor_scorecard (structure=iron_condor, option_selling=true, executable=false). No change. |
| 6.7 | Iron Butterfly | `iron_butterfly_scorecard` | new | no (scorecard) | New short-premium scorecard: sell ATM straddle + buy wings (structure=butterfly, option_selling=true). Distinct from net-debit long_butterfly_scorecard; cross-referenced. |
| 6.8 | Covered Call | `covered_call_income_scorecard` | new | no (scorecard) | New equity scorecard: hold stock, sell 3-7% OTM monthly call. option_selling=true; single-stock instrument, non-executable. Needs a full F&O lot. |
| 6.9 | Cash-Secured Put | `cash_secured_put_scorecard` | new | no (scorecard) | New equity scorecard: sell 5-8% OTM monthly puts with cash reserved; the wheel into 6.8. option_selling=true; single-stock, non-executable. |
| 6.10 | Long Straddle / Strangle (event volatility) | `long_straddle_scorecard` | existing | no (scorecard) | Covered by long_straddle_scorecard, long_strangle_scorecard, event_volatility_long_vega_scorecard. No change. |
| 6.11 | Calendar Spread | `calendar_spread_scorecard` | existing | no (scorecard) | Covered by calendar_spread_scorecard (structure=calendar, non-executable). Note SEBI removed expiry-day calendar margin benefit. No change. |
| 6.12 | Diagonal / PMCC | `diagonal_pmcc_scorecard` | new | no (scorecard) | New scorecard: long 0.7-0.8 delta far-month call + short near OTM calls. Embedded short near leg -> option_selling=true, structure=calendar, non-executable. |
| 6.13 | Ratio Spreads and Backspreads | `call_ratio_backspread_scorecard` | enriched | no (scorecard) | Enriched to contrast net-long-convexity backspread (margin-light, small defined cost) vs net-short front ratio (naked-tail, 'strangle in disguise'). Stays structure=ratio, option_selling=true. |
| 6.14 | Expiry-Day Playbook (Tuesday Nifty) | `expiry_day_directional_defined_risk` | enriched | yes | Enriched with post-Nov-2024 single-weekly regime, morning theta-pin (scorecard prose) vs afternoon gamma-trend split, +2% ELM, square ITM before close. Executable path is long/defined-risk afternoon script only. |
| 6.15 | Delta-Neutral / Gamma Scalping | `delta_neutral_gamma_scalping_scorecard` | new | no (scorecard) | New scorecard: long ATM straddle + continuous futures delta re-hedging. option_selling=false (long the straddle) but structure=straddle and live-hedge requirement force non-executable. |
| 7.1 | Positional Futures Trend Trading | `positional_futures_trend_scorecard` | new | no (scorecard) | NEW futures-desk scorecard. Section 4 trend signals (4.3 MA crossover, 4.4 Supertrend/ADX) on one index-future lot per signal; Supertrend or 2.5-3 ATR trail, roll 1-2 days before expiry. single_leg / directional. Non-executable: ~Rs 16-18 lakh notional and undefined overnight-gap/mark-to-market risk per lot, ~Rs 2-2.6 lakh margin, Rs 10 lakh+ capital under the 1% rule. |
| 7.2 | Cash-Futures Arbitrage (basis capture) | `cash_futures_basis_arbitrage_scorecard` | new | no (scorecard) | NEW futures-desk scorecard. Buy cash + short future 1:1 when annualised basis clears funding+costs; unwind at convergence near expiry or roll. structure none / market_neutral. Non-executable: two-market legs, futures margin, single-digit-annualised return decided by cost/slippage; productised by arbitrage funds with equity-fund taxation. |
| 7.3 | Futures Calendar Spreads | `futures_calendar_spread_scorecard` | new | no (scorecard) | NEW futures-desk scorecard. Long one month / short another of the same underlying when the near-far spread deviates from historical carry; exit on normalisation or before near-month expiry. calendar / market_neutral. Distinct from the OPTIONS calendar_spread_scorecard (that trades IV term structure at one strike). Non-executable: leveraged spread risk, SEBI removes expiry-day spread-margin benefit, low spread-vol invites sizing creep. |

## Coverage summary

- **Sections mapped: 49/49** — every strategy section in the compendium is
  reconciled to a registry strategy id (Chapters 3-7).
- **By reconciliation status (distinct strategy ids):** 25 new, 11 enriched (rewritten in place), 15 already existing.
  The 49 section rows collapse two multi-id rows (4.6 -> two new Bollinger entries, 5.5 -> two enriched gap entries), so the 49 rows correspond to 25 + 11 = 36 upserts.
- **Total strategies in the registry: 93** — 46 executable, 47 scorecard-only.
- **Desk distribution:**

| Desk | Total | Executable | Scorecard-only |
| --- | ---: | ---: | ---: |
| Options | 31 | 12 | 19 |
| Equities | 32 | 27 | 5 |
| Investment | 27 | 7 | 20 |
| Futures | 3 | 0 | 3 |

### Safety statement

- `paper_only: true` and `live_orders_enabled: false` hold at the registry root and
  on every single strategy; the loader rejects any file that says otherwise.
- No live orders are placed and no broker/FYERS order-placement, modification, or
  cancellation code exists anywhere in the platform.
- **Short-premium / undefined-risk structures are scorecard-only** (short straddle/
  strangle, iron condor/butterfly, ratio/front-ratio, credit spreads): never executable.
- **Futures strategies are scorecard-only** (Chapter 7): index/stock futures are
  leveraged, undefined-risk, overnight-gap instruments and are never executable.
- **Single-stock (equity) options are never executable** (covered call, cash-secured
  put): only NSE index options may back an executable option strategy. The one new
  executable option strategy, `index_long_call_put_directional` (6.1), is an
  index-only, single-leg, premium-capped defined-risk long.
- Executable strategies are long / debit / defined-risk only and each carries an
  explicit rupee risk block; no strategy ships pre-labelled live-eligible.
