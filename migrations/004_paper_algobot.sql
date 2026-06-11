-- Phase 2A paper algobot.
-- This migration creates paper-trading audit tables only.
-- It does not create any live order placement capability.

create schema if not exists research;

create table if not exists research.paper_trades (
    paper_trade_id bigserial primary key,
    signal_id bigint not null references research.signals(signal_id) on delete restrict,
    signal_run_id bigint not null references research.signal_runs(signal_run_id) on delete restrict,
    symbol text not null,
    side text not null default 'long' check (side in ('long')),
    status text not null check (status in ('pending_entry', 'open', 'closed', 'cancelled', 'skipped')),
    entry_trigger numeric(18,6) not null,
    entry_price numeric(18,6),
    entry_time timestamptz,
    stop_loss numeric(18,6) not null,
    target numeric(18,6) not null,
    quantity integer not null check (quantity >= 0),
    position_value numeric(18,6) not null default 0,
    max_risk numeric(18,6) not null default 0,
    exit_price numeric(18,6),
    exit_time timestamptz,
    realized_pnl numeric(18,6),
    exit_reason text,
    strategy_version text not null default 'paper_algobot_v1',
    live_orders_enabled boolean not null default false check (live_orders_enabled = false),
    notes text,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(signal_id)
);

create table if not exists research.paper_trade_events (
    event_id bigserial primary key,
    paper_trade_id bigint not null references research.paper_trades(paper_trade_id) on delete cascade,
    event_type text not null,
    event_time timestamptz not null default now(),
    price numeric(18,6),
    quantity integer,
    message text not null,
    raw jsonb not null default '{}'::jsonb
);

create table if not exists research.risk_state (
    risk_state_id bigserial primary key,
    week_start date not null unique,
    capital numeric(18,6) not null,
    max_risk_per_trade numeric(18,6) not null,
    max_weekly_loss numeric(18,6) not null,
    max_open_positions integer not null,
    realized_pnl numeric(18,6) not null default 0,
    paper_only boolean not null default true check (paper_only = true),
    live_orders_enabled boolean not null default false check (live_orders_enabled = false),
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists paper_trades_status_idx on research.paper_trades(status, created_at desc);
create index if not exists paper_trades_symbol_idx on research.paper_trades(symbol, created_at desc);
create index if not exists paper_trade_events_trade_idx on research.paper_trade_events(paper_trade_id, event_time desc);
