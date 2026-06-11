-- Multi-strategy NSE intraday options strategy pack tables.
-- Paper/proxy-research only. No live broker order capability is created.

create schema if not exists research;

create table if not exists research.strategy_pack_campaigns (
    campaign_id bigserial primary key,
    name text not null unique,
    start_date date not null default current_date,
    end_date date,
    paper_only boolean not null default true check (paper_only = true),
    live_orders_enabled boolean not null default false check (live_orders_enabled = false),
    active boolean not null default true,
    config_sha256 text not null,
    notes text,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists research.strategy_pack_allocations (
    allocation_id bigserial primary key,
    campaign_id bigint not null references research.strategy_pack_campaigns(campaign_id) on delete cascade,
    strategy_id text not null,
    strategy_name text not null,
    paper_capital numeric(18,6) not null check (paper_capital > 0),
    max_trade_loss numeric(18,6) not null check (max_trade_loss > 0),
    max_daily_loss numeric(18,6) not null check (max_daily_loss > 0),
    max_premium_exposure numeric(18,6) not null check (max_premium_exposure > 0),
    enabled boolean not null default true,
    paper_trade_enabled boolean not null default true,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(campaign_id, strategy_id)
);

create table if not exists research.strategy_pack_paper_trades (
    pack_trade_id bigserial primary key,
    campaign_id bigint not null references research.strategy_pack_campaigns(campaign_id) on delete restrict,
    strategy_id text not null,
    strategy_name text not null,
    underlying text not null,
    underlying_symbol text not null,
    direction text not null,
    structure text not null,
    status text not null check (status in ('open', 'closed', 'cancelled', 'skipped')),
    signal_reason text not null,
    entry_time timestamptz not null,
    entry_underlying numeric(18,6) not null,
    entry_proxy_premium numeric(18,6),
    risk_rupees numeric(18,6) not null,
    max_loss_rupees numeric(18,6) not null,
    target_r numeric(18,6) not null default 2,
    stop_underlying numeric(18,6),
    target_underlying numeric(18,6),
    exit_time timestamptz,
    exit_underlying numeric(18,6),
    realized_pnl numeric(18,6),
    exit_reason text,
    paper_only boolean not null default true check (paper_only = true),
    live_orders_enabled boolean not null default false check (live_orders_enabled = false),
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists strategy_pack_paper_trades_campaign_status_idx
    on research.strategy_pack_paper_trades(campaign_id, status, strategy_id, entry_time desc);
create index if not exists strategy_pack_paper_trades_strategy_time_idx
    on research.strategy_pack_paper_trades(strategy_id, entry_time desc, status);

create table if not exists research.strategy_pack_paper_trade_events (
    event_id bigserial primary key,
    pack_trade_id bigint not null references research.strategy_pack_paper_trades(pack_trade_id) on delete cascade,
    event_type text not null,
    event_time timestamptz not null default now(),
    message text not null,
    raw jsonb not null default '{}'::jsonb
);

create index if not exists strategy_pack_paper_trade_events_trade_idx
    on research.strategy_pack_paper_trade_events(pack_trade_id, event_time desc);
