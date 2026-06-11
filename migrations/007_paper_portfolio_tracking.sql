-- Paper portfolio tracking for day-on-day fictitious capital growth.
-- Research/paper-only. No live broker order capability is created.

create schema if not exists research;

create table if not exists research.paper_portfolio_runs (
    portfolio_run_id bigserial primary key,
    name text not null unique,
    start_date date not null,
    starting_capital numeric(18,6) not null check (starting_capital > 0),
    active boolean not null default true,
    paper_only boolean not null default true check (paper_only = true),
    live_orders_enabled boolean not null default false check (live_orders_enabled = false),
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists research.paper_portfolio_daily_snapshots (
    snapshot_id bigserial primary key,
    portfolio_run_id bigint not null references research.paper_portfolio_runs(portfolio_run_id) on delete cascade,
    snapshot_date date not null,
    starting_capital numeric(18,6) not null,
    realized_pnl numeric(18,6) not null default 0,
    unrealized_pnl numeric(18,6) not null default 0,
    equity numeric(18,6) not null,
    open_positions integer not null default 0,
    pending_positions integer not null default 0,
    closed_positions integer not null default 0,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(portfolio_run_id, snapshot_date)
);

create index if not exists paper_portfolio_runs_active_idx
    on research.paper_portfolio_runs(active, start_date desc);

create index if not exists paper_portfolio_daily_snapshots_run_date_idx
    on research.paper_portfolio_daily_snapshots(portfolio_run_id, snapshot_date desc);
