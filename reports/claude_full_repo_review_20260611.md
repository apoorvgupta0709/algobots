# Full Repo Trading-Logic Review — 2026-06-11

Reviewed at commit `3815ee9` (head of `origin/main` plus REVIEW_GUIDE.md). Scope: the five core scripts named in REVIEW_GUIDE.md, their configs, the test suite, and the backtest/tuning evidence under `reports/`. Method: the BankNifty engine was read line-by-line; the NSE strategy pack and the gate/backtest-evidence scopes were each reviewed by an independent agent; safety claims were grep-verified.

## Test suite

`uv sync && uv run pytest -q`: **177 passed, 7 failed in 6.51s**. All 7 failures are `psycopg.OperationalError: connection refused` on `127.0.0.1:55432` — they require the local Postgres that is intentionally excluded from the repo. 177 + 7 = 184, consistent with the previously verified "184 passed" with the DB running. No logic failures.

## Safety posture — PASS across the board

- **No order-placement code exists anywhere in the repo.** Grep across `scripts/`, `dashboard/`, `tests/` for `place_order`, `modify_order`, `cancel_order`, basket orders, and POST endpoints finds only read-only FYERS usage (auth, quotes, history, orderbook/position *reads*). `tests/test_fyers_trading_snapshots.py` asserts `place_order` is never called; `tests/test_watchlist_daily_report.py:216` greps source for forbidden patterns.
- **Both engines refuse to run unless `paper_only: true` and `live_orders_enabled: false`**, parsed with strict booleans that reject strings (`scripts/banknifty_options_paper.py:350-360,793-796`; pack `strict_bool`).
- **Cap math is enforced at config load**: `max_trades_per_day × max_trade_loss ≤ max_daily_loss` (3 × ₹1,500 = ₹4,500 ≤ ₹5,000) or the engine exits (`banknifty_options_paper.py:798-807`).
- **Live-order gate is fail-closed**: missing/malformed config crashes before anything runs; missing keys default safe (`live_orders_enabled=False`, `kill_switch_enabled=True`); kill switch AND enable flag must both flip, plus exact confirmation text and approval status, re-checked at decision time (`run_live_order_gate.py:110-113,138-150,395-420`). No env-var or CLI backdoor.
- **All findings from the prior multi-agent review (2026-06-10) are verified fixed**: stale-quote force-exit (`banknifty_options_paper.py:1324-1340`), cap math, `dashboard_ro` read-only role (migration 009 + `dashboard/banknifty_options_dashboard.py:38,74`), streamlit pinned, dashboard external-bind guard.

## High-severity findings

### H1 — Pullback v2 backtest entries have ~4 minutes of look-ahead
`run_banknifty_pullback_v2_backtest.py:270-274,479`: the signal is computed from the *complete* 5-minute candle (close/high/low/volume, knowable only at ts+5min) but the fill is the open of the first 1-minute candle after the candle's *start* (ts+1min). Confirmed in the trades CSVs: entries land at :06/:16/:46/:51. For a strategy with single-digit-minute holds, a 4-minute head start on a candle already known to have closed in your direction is plausibly the entire reported edge. The headline +₹100,870 / ~85% win rate / 1.2R expectancy should not be trusted, even directionally, until entries are re-run at signal-candle close.

### H2 — Exit-tuning was in-sample optimization, then re-reported as validation
The 16:47 backtest (baseline exits) **failed** the ≥55% MFE-capture gate. The exit sweep (`reports/banknifty_pullback_v2_exit_tuning_*`) then ran on the *same* 2026-02-01→2026-06-11 window, the winning `balanced_min300_pct30_start1_0` variant was written into `config/banknifty_options_paper.json` (exits block), and the 17:19 re-run on the same data reports "all gates PASS" — matching the tuning JSON to the rupee. That is selecting on the test set, not validation. The first sweep also had a units bug (`ratchet_giveback_pct` 0.35 vs 35 — a 0.35% giveback, i.e. a degenerate near-perfect exit); the `_corrected_pct_` rerun fixed units but kept the degenerate variant, whose table-best 1.32R expectancy is itself a red flag for the fill model. The sweep tooling is not in the repo (not reproducible), and report paths point to `/opt/data/finance-db/` — another host.

### H3 — Both proxy P&L models are structurally optimistic
- No theta decay (fatal for the expiry-Tuesday long-options strategy — a flat day scores ~0R instead of bleeding), no IV moves, no slippage; pullback v2 also models **zero transaction costs** across 116 trades.
- Stops fill exactly at the stop level — gaps through the stop are clamped to −1R, so every recorded loss is exactly −1.00R. (The live paper monitor shares this: `evaluate_option_exit` at `banknifty_options_paper.py:1311-1314` fills at `stop_premium`, not the breaching LTP.)
- The NSE pack simulator pays +2R targets that a real debit spread cannot reach: the Nifty bull-call spread (debit ₹22, lot 65, risk ₹1,430) has max structural value ≈ +1.27R, yet winners are scored at +2R (`run_nse_intraday_options_strategy_pack.py:195-264,276`).

### H4 — NSE pack: dead filter + stale-fill tick mode
- `breached_both_sides` (`nse_intraday_options_strategy_pack.py:204-206`) tests whether any OR candle exceeds the max/min computed *from the same candles* — a tautology that can never fire. The ORB card's "skip if OR breached both sides" filter is dead code. No test covers it.
- Tick mode (`run_nse_intraday_options_strategy_pack.py:559-628`) re-scans the whole day and opens paper trades at the *first* qualifying candle's price — hours stale by tick time — and after a close it re-detects the same historical signal and re-opens up to `max_trades_per_day` times. The paper campaign's fills are not achievable executions.

### H5 — NSE pack backtest never exercises its regime gates
VIX, option premiums/debits, spread quality, and earnings flags are hardcoded constants in `evaluate_day` (`run_nse_intraday_options_strategy_pack.py:276-328`: `vix=15/16/18`, `earnings_today=False`). The cards' VIX-band, spread-quality, and earnings-exclusion gates therefore filter nothing: high-VIX panic days and earnings days are all included.

## Medium-severity findings

- **NSE pack config knobs that are decorative**: `max_daily_loss`, `max_premium_exposure`, `global_max_open_positions`, `force_exit_time`, `no_new_entries_before` are parsed and persisted but never enforced at runtime; only per-strategy `max_open_positions`/`max_trades_per_day` are (`run_nse_intraday_options_strategy_pack.py:601-603`). In practice unreachable today (≤1 signal/strategy/day), but the config lies about what is enforced. `max_trade_loss` is likewise not plumbed into the strategy logic (hardcoded 1500 default at `nse_intraday_options_strategy_pack.py:185-189`).
- **Single-stock momentum relative-strength filter is wrong for shorts** (`nse_intraday_options_strategy_pack.py:491`): it requires the stock to *outperform* the index by +0.2% even on a breakdown short. Short signals are suppressed or perverse.
- **CPR bias uses the 09:15 5-minute candle**, not the card's first 15-minute close (`nse_intraday_options_strategy_pack.py:298-301`); the pivot-cross abort filter is unimplemented.
- **VWAP mean reversion uses ±1σ instead of the card's 2σ** (`nse_intraday_options_strategy_pack.py:438-439`), σ of session closes rather than VWAP deviations; the backtest labels every non-narrow-CPR day a "range day," exactly the mislabeling the card's own validation gate warns about. Index candles carry no volume, so the "VWAP" degrades to a typical-price average.
- **String-boolean truthiness hazards**: per-strategy `enabled`/`paper_trade_enabled` use `bool()` — the string `"false"` enables a strategy (`nse_intraday_options_strategy_pack.py:152-153`); same pattern on the live gate's `live_orders_enabled` (`run_live_order_gate.py:141`), where a string `"false"` would *enable* the flag. Harmless today (no order code behind it), but must be strict-bool before any execution phase.
- **BankNifty config drift hazard**: the nested `risk` block (`max_trade_loss_inr` etc.) in `config/banknifty_options_paper.json` is never read — only the top-level keys are (`load_config`); editing the nested block changes nothing.
- **Timezone latency**: `ts::date = current_date` SQL and `date.today()` resolve in DB-session/machine timezone. Correct on this IST Mac; wrong after 18:30 UTC if ever deployed on a UTC host.
- **Paper-campaign vs backtest exits diverge**: the pack's open-trade closer checks only the latest candle at tick time (misses intertick stop/target touches) and omits the ₹120–₹250 cost the backtest subtracts (`run_nse_intraday_options_strategy_pack.py:508-530`).
- **Backtest evidence is statistically thin**: pullback v2 = 116 trades / 88 days / one regime (win-rate CI ±6.5%); NSE pack = 156 trades across 5 strategies — the celebrated `expiry_tuesday_directional` PF 3.50 rests on **6 trades** and is noise. The pack's honest aggregate is −₹10,773.

## Low / informational

- Expiry-"Tuesday" is a weekday check, holiday-naive; `next_tuesday_expiry` is dead code.
- Lot sizes and debits are hardcoded historical constants; no strike selection exists in the pack (consistent with "proxy" framing).
- Live gate: `draft_from_idea_row` defaults a missing `max_loss_amount` to 0, which passes the risk-cap check; `--config` accepts any path.
- `evaluate_day` hard-indexes all five strategy ids — a config omitting one passes `validate()` then KeyErrors.
- Pack tests are happy-path only: none of the high findings above would be caught by the current suite.
- Daily max-drawdown in pullback v2 is computed on daily aggregates (understates intraday DD) and hardcodes the ₹5k/₹15k thresholds.

## What is genuinely good

- The BankNifty engine's live guardrails are real and layered: entry windows, trade/position caps, daily-loss lockout before every scan (`banknifty_options_paper.py:2234-2243`), risk-based sizing that *rejects* trades whose structural risk exceeds ₹1,500 rather than widening stops, burned-level re-entry block after a full −1R stop, stale-quote breakeven force-exit, spread filter, and a complete per-trade audit JSON.
- Stop-before-target intra-candle ordering is conservative and documented in both backtests.
- Reports disclose windows, trade counts, and proxy caveats; the NSE pack report publishes a negative aggregate rather than cherry-picking.
- Config numbers match the documented limits exactly (₹50,000 / ₹1,500 / ₹5,000 / ₹40,000 / 3 / 1 / 5 / 09:30-09:35 / 14:45 / 15:20).

## Bottom line

**Safe to keep paper trading — nothing here can place a live order, and the rupee guardrails on the BankNifty engine are genuinely enforced.** But the performance evidence is not yet believable: the pullback v2 headline is built on a 4-minute look-ahead entry, frictionless theta-free fills, and exit parameters tuned on the very window used to declare the gates passed. Before treating any backtest number as edge: (1) fix the entry to signal-candle close, (2) add costs/slippage/theta drag, (3) validate tuned exits on data the sweep never saw (walk-forward or a held-out month), (4) fix the dead `breached_both_sides` filter and the short-side relative-strength bug, and (5) replace `bool()` with strict-bool on every safety-relevant flag before any Phase-3 execution work.
