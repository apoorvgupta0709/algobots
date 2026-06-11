-- Finance + market-data schema for local PostgreSQL
-- Original external payloads are kept in raw jsonb columns for auditability.

create schema if not exists finance;
create schema if not exists market;

create table if not exists finance.accounts (
    account_id bigserial primary key,
    account_name text not null unique,
    account_type text not null,
    provider text,
    currency text not null default 'INR',
    is_active boolean not null default true,
    created_at timestamptz not null default now()
);

create table if not exists finance.transactions (
    transaction_id bigserial primary key,
    account_id bigint references finance.accounts(account_id),
    txn_date date not null,
    description text not null,
    amount numeric(18,4) not null,
    currency text not null default 'INR',
    category text,
    subcategory text,
    counterparty text,
    source text,
    external_id text,
    raw jsonb,
    created_at timestamptz not null default now()
);

create unique index if not exists transactions_dedupe_idx
    on finance.transactions(account_id, txn_date, amount, description, coalesce(external_id, ''));
create index if not exists transactions_txn_date_idx on finance.transactions(txn_date);
create index if not exists transactions_category_idx on finance.transactions(category, subcategory);

create table if not exists market.instruments (
    symbol text primary key,                 -- e.g. NSE:SBIN-EQ
    exchange text,
    name text,
    asset_type text,
    tick_size numeric(18,8),
    lot_size integer,
    fyers_token text,
    raw jsonb,
    updated_at timestamptz not null default now()
);

create table if not exists market.candles (
    symbol text not null references market.instruments(symbol) on delete cascade,
    resolution text not null,                -- FYERS resolution: 1, 5, 15, 60, D, W, M
    ts timestamptz not null,
    open numeric(18,6) not null,
    high numeric(18,6) not null,
    low numeric(18,6) not null,
    close numeric(18,6) not null,
    volume bigint,
    source text not null default 'fyers_v3',
    raw jsonb,
    inserted_at timestamptz not null default now(),
    primary key(symbol, resolution, ts)
);

create index if not exists candles_symbol_resolution_ts_idx on market.candles(symbol, resolution, ts desc);

create table if not exists market.quotes (
    symbol text primary key references market.instruments(symbol) on delete cascade,
    ltp numeric(18,6),
    open numeric(18,6),
    high numeric(18,6),
    low numeric(18,6),
    close numeric(18,6),
    volume bigint,
    quote_time timestamptz,
    raw jsonb,
    updated_at timestamptz not null default now()
);

create table if not exists market.ingestion_runs (
    run_id bigserial primary key,
    source text not null,
    job_type text not null,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    status text not null default 'running',
    rows_inserted integer not null default 0,
    rows_updated integer not null default 0,
    notes text,
    params jsonb
);

create or replace view market.latest_candles as
select distinct on (symbol, resolution)
    symbol, resolution, ts, open, high, low, close, volume
from market.candles
order by symbol, resolution, ts desc;
