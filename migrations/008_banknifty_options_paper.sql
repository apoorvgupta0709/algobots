-- BankNifty options paper campaign tables.
-- Paper/research only. No live broker order capability is created.

create schema if not exists research;

create table if not exists research.option_paper_campaigns (
    campaign_id bigserial primary key,
    name text not null unique,
    underlying text not null default 'BANKNIFTY',
    underlying_symbol text not null default 'NSE:NIFTYBANK-INDEX',
    start_date date not null,
    starting_capital numeric(18,6) not null check (starting_capital > 0),
    active boolean not null default true,
    max_premium_exposure numeric(18,6) not null default 1500,
    max_daily_loss numeric(18,6) not null default 500,
    max_open_positions integer not null default 1 check (max_open_positions >= 0),
    max_trades_per_day integer not null default 2 check (max_trades_per_day >= 0),
    stop_loss_pct numeric(8,6) not null default 0.30,
    target_pct numeric(8,6) not null default 0.50,
    no_new_trades_after time not null default time '14:45',
    force_exit_time time not null default time '15:20',
    poll_interval_seconds integer not null default 15,
    paper_only boolean not null default true check (paper_only = true),
    live_orders_enabled boolean not null default false check (live_orders_enabled = false),
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists research.option_contracts (
    symbol text primary key,
    underlying text not null,
    expiry date not null,
    strike numeric(18,6) not null,
    option_type text not null check (option_type in ('CE', 'PE')),
    lot_size integer not null check (lot_size > 0),
    tick_size numeric(18,6) not null default 0.05,
    raw jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create index if not exists option_contracts_lookup_idx
    on research.option_contracts(underlying, expiry, strike, option_type);

create table if not exists research.option_paper_trades (
    option_trade_id bigserial primary key,
    campaign_id bigint not null references research.option_paper_campaigns(campaign_id) on delete restrict,
    symbol text not null references research.option_contracts(symbol) on delete restrict,
    underlying text not null default 'BANKNIFTY',
    underlying_symbol text not null,
    option_type text not null check (option_type in ('CE', 'PE')),
    expiry date not null,
    strike numeric(18,6) not null,
    side text not null default 'long' check (side in ('long')),
    status text not null check (status in ('open', 'closed', 'cancelled', 'skipped')),
    signal_reason text not null,
    underlying_entry numeric(18,6),
    entry_premium numeric(18,6) not null,
    entry_time timestamptz not null default now(),
    stop_premium numeric(18,6) not null,
    target_premium numeric(18,6) not null,
    highest_premium numeric(18,6),
    lots integer not null check (lots >= 0),
    lot_size integer not null check (lot_size > 0),
    quantity integer not null check (quantity >= 0),
    premium_value numeric(18,6) not null default 0,
    exit_premium numeric(18,6),
    exit_time timestamptz,
    realized_pnl numeric(18,6),
    exit_reason text,
    strategy_version text not null default 'banknifty_options_paper_v1',
    live_orders_enabled boolean not null default false check (live_orders_enabled = false),
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists option_paper_trades_campaign_status_idx
    on research.option_paper_trades(campaign_id, status, created_at desc);
create index if not exists option_paper_trades_symbol_idx
    on research.option_paper_trades(symbol, created_at desc);

create table if not exists research.option_paper_trade_events (
    event_id bigserial primary key,
    option_trade_id bigint not null references research.option_paper_trades(option_trade_id) on delete cascade,
    event_type text not null,
    event_time timestamptz not null default now(),
    premium numeric(18,6),
    quantity integer,
    message text not null,
    raw jsonb not null default '{}'::jsonb
);

create index if not exists option_paper_trade_events_trade_idx
    on research.option_paper_trade_events(option_trade_id, event_time desc);

create table if not exists research.option_paper_daily_snapshots (
    snapshot_id bigserial primary key,
    campaign_id bigint not null references research.option_paper_campaigns(campaign_id) on delete cascade,
    snapshot_date date not null,
    starting_capital numeric(18,6) not null,
    realized_pnl numeric(18,6) not null default 0,
    unrealized_pnl numeric(18,6) not null default 0,
    equity numeric(18,6) not null,
    open_positions integer not null default 0,
    closed_positions integer not null default 0,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique(campaign_id, snapshot_date)
);

create index if not exists option_paper_daily_snapshots_campaign_date_idx
    on research.option_paper_daily_snapshots(campaign_id, snapshot_date desc);
