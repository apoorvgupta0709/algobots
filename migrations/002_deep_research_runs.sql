-- Deep research integration for source-backed external context.
-- Read-only research support only; no order placement or execution path.

create schema if not exists research;

create table if not exists research.deep_research_runs (
    deep_research_run_id bigserial primary key,
    topic text not null,
    symbols text[] not null default '{}'::text[],
    prompt_template text not null,
    query text not null,
    answer text,
    citations jsonb not null default '[]'::jsonb,
    model text not null,
    provider text not null default 'openrouter',
    output_format text not null default 'markdown_report',
    usage jsonb not null default '{}'::jsonb,
    cost numeric(18,8),
    finish_reason text,
    status text not null default 'success' check (status in ('success', 'error', 'dry_run')),
    error text,
    raw jsonb not null default '{}'::jsonb,
    report_path text,
    created_at timestamptz not null default now()
);

create index if not exists deep_research_runs_topic_created_idx
    on research.deep_research_runs(topic, created_at desc);

create index if not exists deep_research_runs_symbols_gin_idx
    on research.deep_research_runs using gin(symbols);

create index if not exists deep_research_runs_template_created_idx
    on research.deep_research_runs(prompt_template, created_at desc);
