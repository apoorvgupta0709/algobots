-- Phase 3 book knowledge: pgvector embeddings + full-text search on chunks.
-- Research-only. No FYERS access, no live order placement code.
-- Requires the pgvector extension binaries (scripts/install_pgvector.sh on the VPS).

create extension if not exists vector;

alter table knowledge.chunks
    add column if not exists embedding vector(384);

alter table knowledge.chunks
    add column if not exists tsv tsvector
    generated always as (to_tsvector('english', content)) stored;

create index if not exists chunks_embedding_hnsw_idx
    on knowledge.chunks using hnsw (embedding vector_cosine_ops);

create index if not exists chunks_tsv_gin_idx
    on knowledge.chunks using gin (tsv);

create table if not exists knowledge.embedding_runs (
    run_id bigserial primary key,
    model_name text not null,
    embedding_dim integer not null,
    sources_processed integer not null default 0,
    chunks_embedded integer not null default 0,
    chunks_skipped integer not null default 0,
    status text not null default 'running' check (status in ('running', 'success', 'error')),
    error_text text,
    raw jsonb not null default '{}'::jsonb,
    started_at timestamptz not null default now(),
    finished_at timestamptz
);

-- Keep the read-only dashboard role working if it exists (migration 009).
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'dashboard_ro') then
        grant usage on schema knowledge to dashboard_ro;
        grant select on all tables in schema knowledge to dashboard_ro;
        alter default privileges in schema knowledge grant select on tables to dashboard_ro;
    end if;
end
$$;
