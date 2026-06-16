# BankNifty Day Pattern Library + After-Market Classifier Plan

Date: 2026-06-16
Status: Plan / build specification
Scope: Research and paper-only analytics. No live orders.

## 0. Correction: exits must not cap profit at ₹500

The BankNifty options strategy must use runner-style trailing exits:

- `fixed_target_exit_enabled: false`
- `profit_lock_trigger: 0`
- `profit_lock_step: 0`
- after `+0.5R`, move the paper stop to breakeven plus one tick / cost proxy;
- after ratchet start, trail using MFE ratchet / structure trailing;
- never describe or implement this as a fixed ₹500 profit cap.

The day-pattern reports must explain “how it could have been played” using this exit model: 0.5R breakeven + MFE trailing/ratchet, not a capped scalp target.

## 1. End goal

Build a BankNifty daywise trend-pattern library that:

1. Classifies every historical/current BankNifty session into one primary class:
   - `trend`
   - `range`
   - `spike_channel`
   - `trending_range`
   - `reversal`
   - `chop`
2. Stores day features, classification, explanation, and similar historical days in PostgreSQL.
3. Generates an after-market Markdown/JSON report answering:
   - What kind of day was today?
   - What evidence supports the classification?
   - Which past days were most similar?
   - How could this day have been played in paper/research terms?
   - Which entry/exit filters would have helped or avoided mistakes?
4. Later runs as a daily post-market cron job.
5. Keeps ML/AI experimental until proven. Deterministic rules + nearest-neighbor library are the default production path.

## 2. Existing repo conventions to follow

From repo inspection:

- Main code style is script-based under `scripts/`.
- PostgreSQL migrations are numbered under `migrations/`.
- Tests live directly under `tests/test_*.py`.
- Use `uv run ...` for Python commands.
- Safety invariant: paper-only; no live FYERS order placement.
- Existing data sources:
  - `market.candles` for 1m/5m/D candles;
  - `market.option_chain_summary` and `market.option_chain_snapshots` from option-chain ingestion;
  - `config/banknifty_options_paper.json` for BankNifty constituents and weights;
  - `research.option_paper_trades` for paper-trade post-analysis;
  - `knowledge.*` for trading-book / strategy-card context.

## 3. Files to create

### Core library

Create `scripts/banknifty_trend_patterns.py`.

Responsibilities:

- Pure dataclasses and pure functions; no DB writes inside feature/classifier helpers.
- Dataclasses:
  - `DaySegment`
  - `BankNiftyDayFeatures`
  - `PatternClassification`
  - `SimilarDay`
- Functions:
  - `build_day_features(...)`
  - `classify_day_rules(...)`
  - `feature_vector(...)`
  - `find_nearest_similar_days(...)`
  - `summarize_playbook(...)`

### Historical/current-day builder

Create `scripts/build_banknifty_trend_pattern_library.py`.

CLI:

```bash
uv run python scripts/build_banknifty_trend_pattern_library.py \
  --from 2025-06-01 --to 2026-06-16 --resolution 5 --print
```

Args:

- `--from YYYY-MM-DD`
- `--to YYYY-MM-DD`
- `--date YYYY-MM-DD` as single-day shortcut
- `--resolution 5`
- `--dry-run`
- `--limit`
- `--print`
- `--config config/banknifty_trend_patterns.json`

### After-market report

Create `scripts/generate_banknifty_trend_pattern_report.py`.

CLI:

```bash
uv run python scripts/generate_banknifty_trend_pattern_report.py \
  --date 2026-06-16 --print
```

Outputs:

- `reports/banknifty_trend_patterns/YYYY-MM-DD_BANKNIFTY.md`
- `reports/banknifty_trend_patterns/YYYY-MM-DD_BANKNIFTY.json`

Report sections:

1. Classification: class, direction, confidence.
2. Evidence: ORB, VWAP, close location, breadth, realized vol, option IV/OI availability.
3. Similar days: top 5 historical matches with similarity scores and short notes.
4. How it could have been played:
   - trend/spike-channel: ORB hold, VWAP/pullback continuation, runner trailing;
   - range/chop: avoid breakout chasing; only defined range play if tested;
   - reversal: failed ORB/VWAP reclaim/reject;
   - always paper/research wording, not live advice.
5. Bot lessons:
   - allowed/blocked entries;
   - no-chase guard observations;
   - exit model: 0.5R breakeven + MFE trailing/ratchet, no fixed ₹500 cap.

### Cron wrapper for later

Create `scripts/banknifty_trend_pattern_report.sh`.

Style should match existing wrappers:

- `cd /opt/data/finance-db`
- `FYERS_LOG_PATH=/tmp/`
- `uv run python ...`
- `flock` lockfile to prevent overlap
- prints report path
- no order/execution calls

Do not schedule cron until the first historical backfill/report is verified.

### Config

Create `config/banknifty_trend_patterns.json`.

All thresholds must be config-driven, including:

- ORB window: 15/30 min;
- segment windows: open-drive, midday, close;
- VWAP cross thresholds;
- close-location thresholds;
- trend/range/chop thresholds;
- breadth thresholds;
- realized-vol lookback;
- nearest-neighbor feature weights;
- similarity top-k.

### Tests

Create:

- `tests/test_banknifty_trend_patterns.py`
- `tests/test_banknifty_trend_pattern_library.py`
- `tests/test_banknifty_trend_pattern_report.py`
- `tests/test_banknifty_trend_pattern_schema.py`

## 4. Files to modify

- `README.md`: add commands and paper-only note.
- `CLAUDE.md`: add the new library/report commands under data analysis.
- `docs/plans/trading-system-roadmap.md`: add this as the day-regime pattern-library slice.
- Optional later: `scripts/banknifty_options_paper.py` can attach latest day-pattern context into trade `raw` as advisory context only. It must not place orders and must not override paper safety.

## 5. Migration

Create `migrations/016_banknifty_trend_patterns.sql`.

### `research.banknifty_day_features`

Fields:

- `feature_id bigserial primary key`
- `session_date date not null unique`
- `underlying text not null default 'BANKNIFTY'`
- `underlying_symbol text not null default 'NSE:NIFTYBANK-INDEX'`
- `resolution text not null default '5'`
- OHLC fields
- `gap_pct numeric`
- `day_return_pct numeric`
- `day_range_pct numeric`
- `orb_high numeric`
- `orb_low numeric`
- `orb_range_pct numeric`
- `close_location numeric`
- `vwap_cross_count integer`
- `vwap_side_pct numeric`
- `realized_vol numeric`
- `range_vs_adr10 numeric`
- `weighted_positive_breadth_pct numeric`
- `weighted_negative_breadth_pct numeric`
- `weighted_vwap_confirm_pct numeric`
- nullable option-chain fields: `atm_iv`, `iv_regime`, `pcr`, `max_pain_distance_pct`
- `segments jsonb not null default '[]'`
- `features jsonb not null default '{}'`
- `source text not null default 'banknifty_trend_pattern_engine'`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

### `research.banknifty_day_classifications`

Fields:

- `classification_id bigserial primary key`
- `session_date date not null unique`
- FK/reference to `research.banknifty_day_features(session_date)` if practical
- `primary_class text not null check (...)`
- `direction text check (direction in ('bullish','bearish','neutral','mixed'))`
- `confidence numeric(5,4)`
- `rule_version text not null`
- `algorithm text not null default 'deterministic_rules'`
- `secondary_tags text[] not null default '{}'`
- `explanation jsonb not null default '{}'`
- `similar_days jsonb not null default '[]'`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

### `research.banknifty_day_pattern_reports`

Fields:

- `report_id bigserial primary key`
- `session_date date not null`
- `classification_id bigint references research.banknifty_day_classifications(classification_id)`
- `report_path text`
- `markdown text not null`
- `generated_at timestamptz not null default now()`

Grant `select` to `dashboard_ro` if the role exists.

## 6. Feature engineering

Use 5-minute candles as primary input and daily candles for context.

### Index features

- gap %
- open-to-close %
- high-low range %
- ATR/ADR percentile
- first 15/30-minute ORB high/low/range
- ORB breakout direction and hold/failure
- VWAP distance and VWAP side %
- VWAP cross count
- close location value: `(close - low) / (high - low)`
- slope by day segment
- MFE/MAE from open and from ORB breakout
- day high/low timing
- range expansion by segment

### Constituent breadth

From `config/banknifty_options_paper.json` constituents:

- weighted positive/negative breadth;
- top positive/negative contributors;
- weighted share above/below VWAP;
- breadth divergence vs index direction;
- relative volume where candle volume exists.

### Option-chain context

Use `market.option_chain_summary` / snapshots when available:

- ATM IV;
- IV regime;
- PCR;
- max-pain distance;
- OI buildup.

Missing option-chain data must not fail classification. Store availability warnings.

### Realized volatility

- intraday 5m realized vol;
- daily 10/20-day realized vol;
- current range vs ADR/ATR;
- segment vol: open-drive, midday, close.

### Day segments

- `open_drive`: 09:15-10:15
- `midday`: 10:15-13:30
- `close`: 13:30-15:30

For each segment:

- return %;
- range %;
- VWAP side %;
- net direction;
- volume share;
- close location.

## 7. Classification rules v1

Rules should be interpretable first.

### `trend`

- strong open-to-close direction;
- close near day extreme;
- low VWAP cross count;
- ORB break holds;
- breadth confirms direction.

### `spike_channel`

- large early impulse;
- shallow pullbacks;
- channel continuation;
- close in direction.

### `trending_range`

- directional bias;
- price spends most time one side of VWAP;
- but multiple pullbacks/rotations overlap enough that straight trend-following needs patience.

### `range`

- balanced close location;
- contained inside ORB/prior value area;
- multiple VWAP crosses;
- low range expansion.

### `reversal`

- early directional attempt fails;
- VWAP/ORB breaks opposite way;
- close in opposite third of range.

### `chop`

- low net movement;
- high VWAP crosses;
- low breadth conviction;
- poor follow-through;
- conflicting segment slopes.

## 8. Algorithm exploration roadmap

Production path v1:

1. deterministic rules;
2. nearest-neighbor similar-day library.

Experiment-only, never default until validated:

1. clustering: KMeans / GaussianMixture / hierarchical over normalized feature vectors;
2. HMM/sequence states: intraday above/below VWAP, candle direction, volatility state, range expansion, breadth state;
3. tree models: DecisionTree / RandomForest trained on rule-seeded or manual labels;
4. gradient boosting: sklearn histogram gradient boosting or LightGBM/XGBoost only if dependency approval is explicit.

Promotion gates for ML:

- enough labelled history;
- out-of-sample agreement with manual/review labels;
- confusion matrix inspected;
- model explanation fields generated;
- no direct live/paper order decisions from experimental model.

## 9. Build loop: Claude Code + Codex

Current environment note: Claude Code CLI was not available in this finance profile during planning; Codex was available. Before executing the loop, install/authenticate Claude Code or run the Claude review step externally.

### Loop roles

- Claude Code: architecture/review agent.
  - Checks research quality, safety language, rule interpretability, and repo fit.
  - Writes/updates task breakdown.
- Codex: implementation agent.
  - Implements one slice at a time.
  - Runs tests.
  - Reports diff and failures.

### Slice 1: pure feature/classifier library

Claude prompt:

```text
Read CLAUDE.md and docs/plans/2026-06-16-banknifty-day-pattern-library.md.
Review Slice 1 scope only: implement pure feature extraction, deterministic rules, similar-day vector helpers, and unit tests. Do not add DB writes yet. Ensure exit language says 0.5R breakeven + MFE ratchet, not fixed ₹500 cap.
```

Codex prompt:

```text
Implement Slice 1 from docs/plans/2026-06-16-banknifty-day-pattern-library.md: scripts/banknifty_trend_patterns.py and tests/test_banknifty_trend_patterns.py. Use Decimal for money/percent math where relevant. No DB writes. Run targeted pytest and report results.
```

Gate:

```bash
uv run pytest tests/test_banknifty_trend_patterns.py -q
```

### Slice 2: migration + persistence CLI

Create migration and builder CLI. Gate:

```bash
./scripts/psql.sh -f migrations/016_banknifty_trend_patterns.sql
./scripts/psql.sh -f migrations/016_banknifty_trend_patterns.sql
uv run pytest tests/test_banknifty_trend_pattern_schema.py tests/test_banknifty_trend_pattern_library.py -q
```

### Slice 3: daily report

Create report generator and tests. Gate:

```bash
uv run pytest tests/test_banknifty_trend_pattern_report.py -q
uv run python scripts/generate_banknifty_trend_pattern_report.py --date 2026-06-16 --print
```

### Slice 4: historical backfill and sample analysis

Run:

```bash
uv run python scripts/build_banknifty_trend_pattern_library.py \
  --from 2025-06-01 --to 2026-06-16 --resolution 5 --print
```

Gate:

- every day with enough candles has one primary class;
- missing option chain is warned, not guessed;
- similar-day list populated where history exists;
- sample report generated and reviewed.

### Slice 5: cron wrapper only after verification

Create wrapper but do not schedule until backfill/report is verified.

Later Hermes cron intent:

- run after market close, e.g. 16:00 IST;
- generate report;
- deliver summary + report file to Telegram;
- no live orders.

## 10. Final verification before saying ready

Run:

```bash
uv run pytest -q
python3 -m py_compile scripts/banknifty_trend_patterns.py scripts/build_banknifty_trend_pattern_library.py scripts/generate_banknifty_trend_pattern_report.py
```

Also verify:

- `git diff` has no secrets;
- no `live_orders_enabled: true`;
- no order-placement code added;
- reports state paper/research only;
- reports and code use 0.5R breakeven + MFE trailing/ratchet, not fixed ₹500 profit caps.
