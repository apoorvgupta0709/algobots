# Superseded / invalid backtest artifacts

These files were generated **before** the 2026-06-11/12 trading-logic bug
fixes (inverted relative-strength check, dead-code ORB filter, breakeven/ratchet
timing, and a missing round-trip cost line). Their numbers are inflated and do
**not** reflect the current code. They are kept only for history — do not cite
them, tune against them, or use them as evidence of edge.

The `.json`/`.csv` artifacts below cannot carry an inline warning, so they are
indexed here.

## BankNifty pullback continuation v2

| File | Why superseded | Current reference |
|---|---|---|
| `banknifty_pullback_v2_proxy_backtest_20260611_164707.md` | pre-bugfix (~87% win, ~₹101k) | `banknifty_pullback_v2_proxy_backtest_20260612_061414.md` |
| `banknifty_pullback_v2_proxy_backtest_20260611_171900.md` | pre-bugfix | same |
| `banknifty_pullback_v2_proxy_trades_20260611_164707.csv` | trades for a superseded run | `banknifty_pullback_v2_proxy_trades_20260612_061414.csv` |
| `banknifty_pullback_v2_proxy_trades_20260611_171900.csv` | trades for a superseded run | same |
| `banknifty_pullback_v2_exit_tuning_20260611.json` / `.csv` | exit parameters tuned on the inflated pre-bugfix P&L | re-derive on fixed code |
| `banknifty_pullback_v2_exit_tuning_corrected_pct_20260611.json` / `.csv` | same | re-derive on fixed code |
| `banknifty_pullback_v2_proxy_sensitivity_20260611.json` | sensitivity swept on pre-bugfix P&L | re-derive on fixed code |

> The `exits` block in `config/banknifty_options_paper.json` (`breakeven_at_r`,
> `ratchet_start_r`, `ratchet_giveback_pct`, `ratchet_giveback_min_inr`,
> `stagnation_*`) was tuned against the inflated runs above and should be
> re-derived on the fixed code with the new spread/cost-aware P&L before being
> trusted. That re-tuning needs the production candle DB and is out of scope for
> the code-review fix pass.

## Note on the current reference runs

The current reference runs (`..._20260612_061414.md` and the NSE pack
`..._20260612_061416.md`) are honest but still **proxy** backtests: index-move
P&L with a constant beta, no theta / IV crush / gamma, and only a flat cost
assumption. Treat their numbers as directional ("is there a signal?"), not as
an achievable-P&L estimate. See `reports/claude_algobots_critical_review_20260708.md`.
