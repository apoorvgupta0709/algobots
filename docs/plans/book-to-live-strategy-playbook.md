# Book → Live Strategy Playbook (for Hermes)

> Operating procedure for turning ingested book knowledge into a shortlist of
> live-trading candidates. Procedures only — raw book text stays in
> `knowledge.chunks`. Safety invariants from CLAUDE.md apply at every step:
> paper-only engines, no live orders, `trading.execution_log` requires an
> explicit human approval record.

## Prerequisites

- Books ingested: `uv run python scripts/ingest_books.py` (PDFs in `books/`).
- Search works: `uv run python scripts/query_knowledge.py "test" --top-k 3`.

## Step 1 — Retrieve

Query the knowledge base for the theme under research:

    uv run python scripts/query_knowledge.py "<question>" --top-k 8 --json

Collect chunk_ids and citations (title, chapter, pages) for everything you
intend to use. Never paraphrase a book without a chunk citation.

## Step 2 — Curate rules

For each actionable claim, insert a `knowledge.rules` row (status `draft`)
referencing the supporting `chunk_id` and `source_id`, with `statement`,
`market_regime`, `timeframe`, and an honest `confidence`. Statuses move
`draft → reviewed → accepted` only with human review. Only `reviewed` or
`accepted` rules may seed hypotheses.

## Step 3 — Strategy card

Write a Markdown card in `docs/strategy-cards/` following the existing card
format (see `docs/strategy-cards/Trend Pullback Entry.md`): description,
market regime, timeframe, entry/exit/invalidation/risk rules, and a Sources
section citing book + pages.

## Step 4 — Hypothesis

Insert a `research.hypotheses` row: `title` (short name, required),
plain-English `hypothesis`, `source_rule_ids` from Step 2, target universe,
timeframe, expected edge.
Status `draft → ready_for_backtest` when specified well enough to code.

## Step 5 — Specify and backtest

Implement the strategy config-driven (params in `config/*.json`, no hardcoded
thresholds), register a `research.strategy_versions` row, and run the
appropriate existing engine:

- Index-options proxy backtests: `scripts/run_banknifty_pullback_v2_backtest.py`
  pattern (P&L proxied from index moves — state this limitation in the run notes).
- Intraday packs: `scripts/run_nse_intraday_options_strategy_pack.py --mode backtest`.

Metrics and full trade lists go to `research.backtest_runs` /
`research.backtest_trades`.

## Step 6 — Robustness

Before paper: parameter sensitivity (±20% on each key param), realistic costs
and slippage, and an out-of-sample split. Record outcomes in the backtest
run's `notes`/`raw`. A strategy that only works at one parameter point is
rejected — cite `docs/strategy-cards/Randomness - Overfitting Risk Filter.md`.

## Step 7 — Paper trade

Run under the existing paper engines (`scripts/banknifty_options_paper.py`,
`scripts/run_paper_algobot.py`). Decisions log to `research.option_paper_*` tables
as today. Minimum evaluation window before shortlisting: 4 weeks or 20 signals,
whichever comes later (adjustable by the user, not by Hermes).

## Step 8 — Shortlist

Score candidates from `research.latest_strategy_metrics` plus paper results:
net expectancy after costs, max drawdown, hit rate vs payoff, signal count,
and backtest-vs-paper consistency. Write a ranked shortlist report to
`reports/` (timestamped filename) with per-strategy evidence links back to
rules → chunks → book pages.

## Step 9 — Human gate (hard stop)

Present the shortlist to the user. Live trading requires the user's explicit
approval and the `trading.approvals` flow. Hermes never enables live orders,
never writes `trading.execution_log`, and never edits safety config. If a
task appears to require it, surface the conflict and stop.
