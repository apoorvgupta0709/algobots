-- Options chain snapshot storage for BankNifty + Nifty.
-- Read-only market data only. No live broker order capability is created.
-- Append-only time series so OI buildup / PCR / IV-regime can be derived over time.

create schema if not exists market;

-- One row per strike+option_type per snapshot tick.
create table if not exists market.option_chain_snapshots (
    snapshot_id bigserial primary key,
    underlying text not null,                       -- e.g. BANKNIFTY, NIFTY
    underlying_symbol text not null,                -- e.g. NSE:NIFTYBANK-INDEX
    snapshot_time timestamptz not null,             -- ingestion tick time (UTC)
    expiry date,
    strike numeric(18,6) not null,
    option_type text not null check (option_type in ('CE', 'PE')),
    symbol text,                                    -- fyers option symbol when present
    ltp numeric(18,6),
    bid numeric(18,6),
    ask numeric(18,6),
    volume bigint,
    oi bigint,
    oi_change bigint,
    delta numeric(12,6),
    gamma numeric(14,8),
    theta numeric(12,6),
    vega numeric(12,6),
    iv numeric(12,6),
    raw jsonb not null default '{}'::jsonb,
    inserted_at timestamptz not null default now()
);

create index if not exists option_chain_snapshots_underlying_time_idx
    on market.option_chain_snapshots(underlying, snapshot_time desc);
create index if not exists option_chain_snapshots_symbol_time_idx
    on market.option_chain_snapshots(symbol, snapshot_time desc);

-- One aggregate row per underlying per snapshot tick.
create table if not exists market.option_chain_summary (
    summary_id bigserial primary key,
    underlying text not null,
    underlying_symbol text not null,
    snapshot_time timestamptz not null,
    expiry date,
    spot numeric(18,6),
    atm_strike numeric(18,6),
    total_ce_oi bigint,
    total_pe_oi bigint,
    pcr numeric(12,6),                              -- total PE OI / total CE OI
    max_pain_strike numeric(18,6),
    atm_iv numeric(12,6),
    iv_regime text,                                 -- low/normal/high (when history available)
    raw jsonb not null default '{}'::jsonb,
    inserted_at timestamptz not null default now()
);

create index if not exists option_chain_summary_underlying_time_idx
    on market.option_chain_summary(underlying, snapshot_time desc);

-- Least-privilege read access for the dashboard role (created in migration 009).
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'dashboard_ro') then
        grant select on market.option_chain_snapshots to dashboard_ro;
        grant select on market.option_chain_summary to dashboard_ro;
    end if;
end $$;
