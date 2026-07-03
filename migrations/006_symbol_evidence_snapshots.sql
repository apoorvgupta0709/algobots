-- Structured current fundamental/sentiment evidence snapshots.
-- Read-only research support only; no order placement or execution path.

create schema if not exists research;

create table if not exists research.symbol_evidence_snapshots (
    symbol_evidence_snapshot_id bigserial primary key,
    symbol text not null,
    as_of timestamptz not null default now(),
    evidence_source text not null default 'deep_research'
        check (evidence_source in ('deep_research', 'manual', 'filing', 'news', 'transcript')),
    source_run_id bigint references research.deep_research_runs(deep_research_run_id) on delete set null,
    fundamental_label text not null
        check (fundamental_label in ('strong', 'acceptable', 'weak', 'mixed', 'insufficient_data')),
    fundamental_score numeric(5,2) not null check (fundamental_score >= 0 and fundamental_score <= 25),
    sentiment_label text not null
        check (sentiment_label in ('positive', 'neutral', 'negative', 'mixed', 'event_risk', 'insufficient_data')),
    sentiment_score numeric(5,2) not null check (sentiment_score >= 0 and sentiment_score <= 20),
    confidence text not null default 'low' check (confidence in ('low', 'medium', 'high')),
    summary text not null,
    citations jsonb not null default '[]'::jsonb,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique(symbol, evidence_source, source_run_id)
);

create index if not exists symbol_evidence_snapshots_symbol_asof_idx
    on research.symbol_evidence_snapshots(symbol, as_of desc);

create index if not exists symbol_evidence_snapshots_source_run_idx
    on research.symbol_evidence_snapshots(source_run_id);
