-- Morning stock recommendation system.
-- Phase 1 is read-only research/recommendation storage.
-- live_orders_enabled is stored as false by default; no execution code is created here.

create schema if not exists research;

create table if not exists research.signal_runs (
    signal_run_id bigserial primary key,
    run_type text not null default 'morning_recommendations',
    universe text,
    generated_at timestamptz not null default now(),
    live_orders_enabled boolean not null default false,
    deep_research_enabled boolean not null default false,
    params jsonb not null default '{}'::jsonb,
    report_path text,
    status text not null default 'success' check (status in ('success', 'error', 'dry_run')),
    notes text,
    created_at timestamptz not null default now()
);

create table if not exists research.signals (
    signal_id bigserial primary key,
    signal_run_id bigint not null references research.signal_runs(signal_run_id) on delete cascade,
    symbol text not null,
    label text not null check (label in ('buy_candidate_research', 'paper_setup', 'watch', 'needs_review', 'reject')),
    score numeric(6,2) not null,
    technical_score numeric(6,2) not null default 0,
    fundamental_score numeric(6,2) not null default 0,
    sentiment_score numeric(6,2) not null default 0,
    risk_score numeric(6,2) not null default 0,
    entry_condition text,
    stop_loss numeric(18,6),
    target numeric(18,6),
    max_risk_note text,
    reasons jsonb not null default '[]'::jsonb,
    risks jsonb not null default '[]'::jsonb,
    local_context jsonb not null default '{}'::jsonb,
    deep_research_run_id bigint references research.deep_research_runs(deep_research_run_id) on delete set null,
    created_at timestamptz not null default now(),
    unique(signal_run_id, symbol)
);

create index if not exists signal_runs_generated_idx on research.signal_runs(generated_at desc);
create index if not exists signals_run_score_idx on research.signals(signal_run_id, score desc);
create index if not exists signals_symbol_created_idx on research.signals(symbol, created_at desc);
