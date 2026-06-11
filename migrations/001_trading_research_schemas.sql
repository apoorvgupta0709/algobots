-- Phase 1 trading research expansion.
-- Safe-by-default: research, paper trading, and audit tables only.
-- No FYERS live order placement code is created by this migration.

create schema if not exists knowledge;
create schema if not exists research;
create schema if not exists trading;

-- -----------------------------------------------------------------------------
-- Knowledge: books, notes, chunks, concepts, rules, and playbooks.
-- -----------------------------------------------------------------------------
create table if not exists knowledge.sources (
    source_id bigserial primary key,
    title text not null,
    author text,
    source_type text not null default 'book',
    edition text,
    file_path text,
    file_hash text,
    notes text,
    raw jsonb not null default '{}'::jsonb,
    uploaded_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(file_hash)
);

create table if not exists knowledge.chunks (
    chunk_id bigserial primary key,
    source_id bigint not null references knowledge.sources(source_id) on delete cascade,
    chunk_index integer not null,
    chapter text,
    section text,
    page_start integer,
    page_end integer,
    content text not null,
    token_count integer,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique(source_id, chunk_index),
    check (chunk_index >= 0),
    check (page_start is null or page_start >= 0),
    check (page_end is null or page_end >= coalesce(page_start, 0))
);

create index if not exists chunks_source_idx on knowledge.chunks(source_id, chunk_index);

create table if not exists knowledge.concepts (
    concept_id bigserial primary key,
    name text not null unique,
    description text,
    source_chunk_id bigint references knowledge.chunks(chunk_id) on delete set null,
    confidence numeric(5,4) check (confidence is null or (confidence >= 0 and confidence <= 1)),
    status text not null default 'draft' check (status in ('draft', 'reviewed', 'accepted', 'rejected')),
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists knowledge.rules (
    rule_id bigserial primary key,
    source_id bigint references knowledge.sources(source_id) on delete set null,
    chunk_id bigint references knowledge.chunks(chunk_id) on delete set null,
    concept_id bigint references knowledge.concepts(concept_id) on delete set null,
    rule_type text not null default 'trading',
    statement text not null,
    evidence text,
    market_regime text,
    timeframe text,
    confidence numeric(5,4) check (confidence is null or (confidence >= 0 and confidence <= 1)),
    status text not null default 'draft' check (status in ('draft', 'reviewed', 'accepted', 'rejected', 'converted_to_hypothesis', 'converted_to_skill')),
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists rules_status_idx on knowledge.rules(status);
create index if not exists rules_concept_idx on knowledge.rules(concept_id);

create table if not exists knowledge.playbooks (
    playbook_id bigserial primary key,
    name text not null unique,
    description text,
    rule_ids bigint[] not null default '{}'::bigint[],
    universe text,
    timeframe text,
    market_regime text,
    entry_rules jsonb not null default '[]'::jsonb,
    exit_rules jsonb not null default '[]'::jsonb,
    risk_rules jsonb not null default '[]'::jsonb,
    invalidation_rules jsonb not null default '[]'::jsonb,
    status text not null default 'draft' check (status in ('draft', 'reviewed', 'accepted', 'retired')),
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Research: hypotheses, strategy versions, backtests, factors, model outputs.
-- -----------------------------------------------------------------------------
create table if not exists research.hypotheses (
    hypothesis_id bigserial primary key,
    title text not null,
    hypothesis text not null,
    source_rule_ids bigint[] not null default '{}'::bigint[],
    target_universe text,
    timeframe text,
    expected_edge text,
    assumptions jsonb not null default '{}'::jsonb,
    status text not null default 'draft' check (status in ('draft', 'ready_for_backtest', 'backtesting', 'paper', 'accepted', 'rejected', 'retired')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists research.strategy_versions (
    strategy_version_id bigserial primary key,
    hypothesis_id bigint references research.hypotheses(hypothesis_id) on delete set null,
    strategy_name text not null,
    version text not null,
    code_path text,
    config jsonb not null default '{}'::jsonb,
    parameters jsonb not null default '{}'::jsonb,
    assumptions text,
    status text not null default 'draft' check (status in ('draft', 'backtest', 'paper', 'approved_for_live_review', 'retired')),
    created_at timestamptz not null default now(),
    unique(strategy_name, version)
);

create table if not exists research.backtest_runs (
    backtest_run_id bigserial primary key,
    strategy_version_id bigint not null references research.strategy_versions(strategy_version_id) on delete cascade,
    universe text not null,
    resolution text not null,
    start_ts timestamptz not null,
    end_ts timestamptz not null,
    initial_capital numeric(18,4) not null default 0,
    costs jsonb not null default '{}'::jsonb,
    slippage jsonb not null default '{}'::jsonb,
    metrics jsonb not null default '{}'::jsonb,
    status text not null default 'running' check (status in ('running', 'success', 'error')),
    notes text,
    raw jsonb not null default '{}'::jsonb,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    check (end_ts >= start_ts)
);

create index if not exists backtest_runs_strategy_idx on research.backtest_runs(strategy_version_id, started_at desc);

create table if not exists research.backtest_trades (
    backtest_trade_id bigserial primary key,
    backtest_run_id bigint not null references research.backtest_runs(backtest_run_id) on delete cascade,
    symbol text not null,
    side text not null check (side in ('BUY', 'SELL')),
    entry_ts timestamptz not null,
    exit_ts timestamptz,
    entry_price numeric(18,6) not null,
    exit_price numeric(18,6),
    quantity numeric(18,6) not null,
    gross_pnl numeric(18,6),
    net_pnl numeric(18,6),
    reason_entry text,
    reason_exit text,
    raw jsonb not null default '{}'::jsonb,
    check (quantity > 0),
    check (exit_ts is null or exit_ts >= entry_ts)
);

create index if not exists backtest_trades_run_idx on research.backtest_trades(backtest_run_id, entry_ts);

create table if not exists research.factor_snapshots (
    factor_snapshot_id bigserial primary key,
    symbol text not null,
    resolution text not null,
    ts timestamptz not null,
    factors jsonb not null,
    source text not null default 'local_factor_engine',
    strategy_version_id bigint references research.strategy_versions(strategy_version_id) on delete set null,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique(symbol, resolution, ts, source)
);

create index if not exists factor_snapshots_symbol_ts_idx on research.factor_snapshots(symbol, resolution, ts desc);

create table if not exists research.model_outputs (
    model_output_id bigserial primary key,
    strategy_version_id bigint references research.strategy_versions(strategy_version_id) on delete set null,
    symbol text,
    prompt_version text,
    model_name text,
    input_context jsonb not null default '{}'::jsonb,
    output jsonb not null default '{}'::jsonb,
    raw_text text,
    created_at timestamptz not null default now()
);

create or replace view research.latest_strategy_metrics as
select distinct on (sv.strategy_name, sv.version)
    sv.strategy_name,
    sv.version,
    sv.status as strategy_status,
    br.backtest_run_id,
    br.universe,
    br.resolution,
    br.start_ts,
    br.end_ts,
    br.metrics,
    br.status as backtest_status,
    br.finished_at
from research.strategy_versions sv
left join research.backtest_runs br on br.strategy_version_id = sv.strategy_version_id
order by sv.strategy_name, sv.version, br.finished_at desc nulls last, br.started_at desc;

-- -----------------------------------------------------------------------------
-- Trading: read-only snapshots, trade ideas, approvals, and execution audit.
-- Live order rows require an explicit approval_id; order placement code is absent.
-- -----------------------------------------------------------------------------
create table if not exists trading.positions_snapshots (
    snapshot_id bigserial primary key,
    source text not null default 'fyers_v3',
    account_ref text,
    positions jsonb not null,
    raw jsonb not null default '{}'::jsonb,
    captured_at timestamptz not null default now()
);

create table if not exists trading.orderbook_snapshots (
    snapshot_id bigserial primary key,
    source text not null default 'fyers_v3',
    account_ref text,
    orders jsonb not null,
    raw jsonb not null default '{}'::jsonb,
    captured_at timestamptz not null default now()
);

create table if not exists trading.holdings_snapshots (
    snapshot_id bigserial primary key,
    source text not null default 'fyers_v3',
    account_ref text,
    holdings jsonb not null,
    raw jsonb not null default '{}'::jsonb,
    captured_at timestamptz not null default now()
);

create table if not exists trading.funds_snapshots (
    snapshot_id bigserial primary key,
    source text not null default 'fyers_v3',
    account_ref text,
    funds jsonb not null,
    raw jsonb not null default '{}'::jsonb,
    captured_at timestamptz not null default now()
);

create table if not exists trading.trade_ideas (
    idea_id bigserial primary key,
    strategy_version_id bigint references research.strategy_versions(strategy_version_id) on delete set null,
    symbol text not null,
    side text not null check (side in ('BUY', 'SELL')),
    quantity numeric(18,6) not null,
    order_type text not null check (order_type in ('MARKET', 'LIMIT', 'SL', 'SL-M')),
    price numeric(18,6),
    trigger_price numeric(18,6),
    product_type text not null,
    validity text not null default 'DAY',
    stop_loss numeric(18,6),
    target_price numeric(18,6),
    rationale text not null,
    source_snapshot jsonb not null default '{}'::jsonb,
    risk_snapshot jsonb not null default '{}'::jsonb,
    status text not null default 'draft' check (status in ('draft', 'generated', 'review', 'approved', 'rejected', 'expired', 'executed', 'cancelled')),
    created_at timestamptz not null default now(),
    expires_at timestamptz,
    check (quantity > 0),
    check (price is null or price > 0),
    check (trigger_price is null or trigger_price > 0)
);

create index if not exists trade_ideas_status_idx on trading.trade_ideas(status, created_at desc);
create index if not exists trade_ideas_symbol_idx on trading.trade_ideas(symbol, created_at desc);

create table if not exists trading.approvals (
    approval_id bigserial primary key,
    idea_id bigint not null references trading.trade_ideas(idea_id) on delete cascade,
    approved_by text not null,
    confirmation_text text not null,
    account_ref text,
    symbol text not null,
    side text not null check (side in ('BUY', 'SELL')),
    quantity numeric(18,6) not null,
    order_type text not null check (order_type in ('MARKET', 'LIMIT', 'SL', 'SL-M')),
    price numeric(18,6),
    trigger_price numeric(18,6),
    product_type text not null,
    validity text not null default 'DAY',
    max_loss_amount numeric(18,4),
    exit_plan text,
    status text not null default 'approved' check (status in ('approved', 'used', 'revoked', 'expired')),
    raw jsonb not null default '{}'::jsonb,
    approved_at timestamptz not null default now(),
    expires_at timestamptz,
    check (quantity > 0),
    check (price is null or price > 0),
    check (trigger_price is null or trigger_price > 0)
);

create index if not exists approvals_idea_idx on trading.approvals(idea_id, status, approved_at desc);

create table if not exists trading.execution_log (
    execution_id bigserial primary key,
    approval_id bigint not null references trading.approvals(approval_id) on delete restrict,
    idea_id bigint references trading.trade_ideas(idea_id) on delete set null,
    source text not null default 'fyers_v3',
    account_ref text,
    symbol text not null,
    side text not null check (side in ('BUY', 'SELL')),
    quantity numeric(18,6) not null,
    order_type text not null check (order_type in ('MARKET', 'LIMIT', 'SL', 'SL-M')),
    price numeric(18,6),
    trigger_price numeric(18,6),
    product_type text not null,
    validity text not null default 'DAY',
    action text not null check (action in ('dry_run', 'place_order', 'modify_order', 'cancel_order')),
    broker_order_id text,
    api_status text,
    api_message text,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    check (quantity > 0),
    check (price is null or price > 0),
    check (trigger_price is null or trigger_price > 0)
);

create index if not exists execution_log_approval_idx on trading.execution_log(approval_id, created_at desc);
create index if not exists execution_log_symbol_idx on trading.execution_log(symbol, created_at desc);

create or replace view trading.open_trade_ideas as
select
    idea_id,
    strategy_version_id,
    symbol,
    side,
    quantity,
    order_type,
    price,
    trigger_price,
    product_type,
    validity,
    stop_loss,
    target_price,
    rationale,
    status,
    created_at,
    expires_at
from trading.trade_ideas
where status in ('generated', 'review', 'approved')
  and (expires_at is null or expires_at > now())
order by created_at desc;
