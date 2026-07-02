# Indian Markets Strategy Compendium — Gap Analysis Handoff

Source PDF uploaded by Apoorv: `Indian_Markets_Trading_Strategy_Compendium.pdf`.
The PDF was extracted locally for analysis during this task; the raw extracted text is intentionally not committed to avoid publishing the uploaded source document. The auditable repository artifact is the section-by-section mapping in `docs/indian_markets_strategy_compendium_mapping.md`.

## Current registry snapshot before this handoff

- Existing branch: `feat/india-strategy-universe-20260702-075240`
- Current registry file: `config/strategy_universe_india.json`
- Current total strategies: 68
- Desks: {'equities': 25, 'investment': 18, 'options': 25}
- Executable strategies: 40; scorecard-only: 28
- Existing safety posture from report: registry `paper_only=true`, `live_orders_enabled=false`, executable option-selling count 0.

## What the previous Claude run already did

It created a Python-only, paper/research strategy platform with:

- registry validator: `scripts/strategy_registry.py`
- report CLI: `scripts/strategy_platform_report.py`
- qualification engine/config: `scripts/strategy_qualification.py`, `config/strategy_qualification.json`
- dashboard: `dashboard/strategy_platform_dashboard.py`
- docs: `docs/india_strategy_platform.md`
- tests for registry, dashboard, qualification.

Important: that run was based on the earlier generic strategy-universe prompt, **not** this attached PDF. The pending task is to reconcile against the PDF and fill document-specific gaps.

## PDF strategies already reasonably represented

- **3.3 Asset Allocation and Rebalancing** — Covered generically by risk_parity_allocation, drawdown_controlled_allocation, niftybees_goldbees_allocation_scorecard; needs doc-specific passive core/rebalance prose.
- **3.8 Momentum Factor** — Covered by momentum_factor_composite_scorecard / dma momentum variants.
- **3.9 Low-Volatility Factor** — Covered by low_volatility_portfolio_scorecard.
- **3.13 Sector Rotation** — Covered by sector_rotation_positional and etf_momentum_swing.
- **4.1 Darvas / 52-week high** — Covered by darvas_box_52w_high_equity.
- **4.5 RSI Mean Reversion** — Covered by rsi2_uptrend_reversion_equity / large_cap_mean_reversion_swing.
- **4.8 Relative Strength Leaders** — Covered by relative_strength_leader_equity.
- **4.9 Earnings/Event Swing** — Covered as post_earnings_drift_scorecard_equity; could enrich doc specifics.
- **5.1 ORB** — Covered by equity and option ORB strategies.
- **5.2 VWAP Mean Reversion** — Covered by equity + option VWAP mean reversion.
- **5.3 VWAP Trend Rides** — Covered by vwap_trend_equity / cpr_vwap_debit_spread.
- **5.4 CPR Framework** — Covered by cpr_trend_day_equity and cpr_vwap_debit_spread.
- **5.6 Intraday Pullback Continuation** — Covered by trend_continuation_flag_debit_spread and swing_pullback_rising_ma_equity, but should ensure Brooks second-entry/pullback methodology.
- **5.7 Momentum Burst / News Scalps** — Covered by single_stock_momentum_index_confirm / high_rvol_momentum_continuation_equity.
- **6.2 Debit Vertical Spreads** — Covered by BankNifty/Nifty/FINNIFTY debit-spread strategies.
- **6.3 Credit Vertical Spreads** — Covered as scorecard-only credit_spread_directional_scorecard; keep non-executable.
- **6.4 Short Strangle** — Covered as non-executable scorecard.
- **6.5 Short Straddle** — Covered as non-executable scorecard, but add 9:20 variant details if missing.
- **6.6 Iron Condor** — Covered as non-executable scorecard.
- **6.7 Iron Butterfly** — Covered as long_butterfly_scorecard; verify iron-butterfly short-premium remains non-executable.
- **6.10 Long Straddle/Strangle** — Covered as long straddle/strangle scorecards.
- **6.11 Calendar Spread** — Covered as scorecard.
- **6.13 Ratio Spreads/Backspreads** — Covered as scorecard; ensure backspread vs short ratio risk is explicit.
- **6.14 Expiry-Day Playbook** — Covered by expiry_day_directional_defined_risk but enrich Tuesday Nifty / post-2024 weekly-expiry notes.

## Gaps / enrichments to implement from the PDF

- **3.1 Passive Index Core** — Add explicit Nifty 50 / Nifty Next 50 / total-market passive-core investment strategy or scorecard, including India ETF/index-fund notes.
- **3.2 SIP / Rupee-Cost Averaging** — Existing SIP baseline exists but add doc-specific rupee-cost and step-up variants with monthly contribution/rebalance data requirements.
- **3.4 Value Investing (Graham screens)** — Add standalone value/Graham scorecard with valuation, debt, earnings-stability, margin-of-safety fields.
- **3.5 Quality / Coffee-Can** — Add standalone quality/coffee-can compounding scorecard: ROCE/ROE, sales/profit growth, leverage, longevity filters.
- **3.6 GARP** — Add GARP strategy/scorecard: earnings growth plus PEG/valuation discipline.
- **3.7 Dividend Yield** — Add dividend-yield/income scorecard with payout, free cash flow, leverage, dividend history filters.
- **3.10 Magic Formula** — Add Greenblatt India edition scorecard: earnings yield + ROC ranking, liquidity and governance filters.
- **3.11 CANSLIM** — Add CANSLIM growth-momentum scorecard or backtest-ready equity strategy; include C/A/N/S/L/I/M data requirements.
- **3.12 Special Situations** — Add demerger/buyback/open-offer/turnaround scorecard; event-data dependency, not executable by default.
- **4.2 Pullback-to-Trend** — Existing pullback is generic; enrich or add explicit swing pullback strategy with trend MA, first/second pullback, stop under swing low, measured-move exit.
- **4.3 Moving-Average Crossover** — Investment DMA exists; add swing/positional equity MA crossover variant if not already explicit.
- **4.4 Supertrend / ADX** — Add Supertrend/ADX trend-following equity strategy with ATR stop and trend strength gates.
- **4.6 Bollinger Squeeze/Reversion** — Add Bollinger squeeze breakout and/or Bollinger mean-reversion strategy.
- **4.7 Classical Chart Patterns** — Add scorecard for flags, triangles, head-and-shoulders, cup-and-handle; manual-review/backtest-ready only.
- **4.10 Pair Trading / Statistical Arbitrage** — Existing pairs strategy present; enrich with cointegration/z-score/beta hedge detail and mark as scorecard/backtest-safe unless long-short paper semantics are implemented.
- **5.5 Gap Trading** — Existing gap strategies present; ensure explicit gap-and-go and gap-fill variants with opening range, VWAP reclaim, volume/ATR filters.
- **5.8 Range Fade at Support/Resistance** — Add intraday range-fade equity strategy with support/resistance, VWAP, stop beyond range, time stop.
- **6.1 Long Call / Long Put** — Add explicit naked long call/put directional strategy as defined-risk executable only for index options with premium cap; avoid single-stock options.
- **6.8 Covered Call** — Add covered-call income scorecard only; no option selling execution.
- **6.9 Cash-Secured Put** — Add cash-secured-put scorecard only; no option selling execution.
- **6.12 Diagonal / PMCC** — Add diagonal/PMCC scorecard only; identify embedded short option and non-executable status.
- **6.15 Delta-Neutral / Gamma Scalping** — Add advanced gamma-scalping scorecard; non-executable due to hedge/frequency/live-order requirements.
- **7.1 Positional Futures Trend Trading** — Add Futures Desk or scorecard for positional index/stock futures trend; paper/backtest only with margin/roll/slippage model.
- **7.2 Cash-Futures Arbitrage** — Add futures basis/cash-futures arbitrage scorecard; data requirements include futures chain, funding/margin/STT costs.
- **7.3 Futures Calendar Spreads** — Existing calendar spread is options-focused; add futures calendar/basis spread scorecard.


## Implementation requirements for Claude Code

1. Expand `config/strategy_universe_india.json` so the PDF's 49 strategies are explicitly covered, not just vaguely similar.
2. Add a `futures` desk if needed for Chapter 7, or clearly classify futures strategies as `scorecard-only` with instrument `futures`; preserve report/dashboard support for the new desk.
3. Every added/enriched strategy must include: `id`, `name`, `desk`, `family`, `instrument`, `timeframe`, `direction`, `structure`, `executable`, `option_selling`, `lifecycle_status`, `paper_only`, `live_orders_enabled`, `description`, `entry`, `exit`, `filters`, `rationale`, `data_requirements`, `tags`, and `risk` when executable.
4. Keep option selling / embedded short-premium structures non-executable scorecards: short straddle/strangle, iron condor/butterfly, covered call, cash-secured put, credit spreads, calendars/diagonals/PMCC if they require short option legs, ratio spreads unless net-long-risk is proven.
5. Executable options must be defined-risk long-option or debit-spread only, preferably index options. Do not make single-stock options executable.
6. Enrich docs/report/dashboard/tests so coverage is auditable against the attached document.
7. Use Brooks-style proper pullback/second-entry/measured-move/swing-trail language for pullback continuation strategies; no "buy at signal and hold N bars" methodology.
8. Do not deploy/sync to `/opt/data/finance-db`. Work only in this worktree.
9. Do not place trades, call broker order APIs, add secrets, or enable live orders.

## Verification to run before completion

Use `/opt/data/algobots/.venv/bin/python` if this worktree lacks its own `.venv`. If pytest is unavailable there, use `env -u VIRTUAL_ENV uv run --with pytest python -m pytest ...` from the worktree.

Required checks:

```bash
/opt/data/algobots/.venv/bin/python -m py_compile scripts/strategy_registry.py scripts/strategy_platform_report.py scripts/strategy_qualification.py dashboard/strategy_platform_dashboard.py
/opt/data/algobots/.venv/bin/python scripts/strategy_platform_report.py --output reports/strategy_platform_report.md
/opt/data/algobots/.venv/bin/python -m pytest tests/test_strategy_registry.py tests/test_strategy_platform_dashboard.py tests/test_strategy_qualification.py -q
python3 - <<'PY'
from pathlib import Path
forbidden = ['place_order', 'modify_order', 'cancel_order', 'live_orders_enabled": true', 'paper_only": false']
for token in forbidden:
    hits=[]
    for path in Path('.').rglob('*'):
        if path.is_file() and path.suffix in {'.py','.json','.md','.sql','.sh'} and '.git' not in path.parts:
            txt=path.read_text(errors='ignore')
            if token in txt:
                hits.append(str(path))
    print(token, hits[:20])
PY
```

If pytest is missing, install only via uv's ephemeral environment; do not mutate global Python.
