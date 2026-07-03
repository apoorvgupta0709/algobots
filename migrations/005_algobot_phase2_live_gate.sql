-- Phase 2A/2B algobot hardening.
-- Paper-trade lifecycle additions + live-order gate state/audit controls.
-- This migration does not enable live trading and creates no broker order call.

create schema if not exists trading;

alter table research.paper_trades
    add column if not exists initial_stop_loss numeric(18,6),
    add column if not exists highest_price numeric(18,6),
    add column if not exists trail_activated boolean not null default false,
    add column if not exists time_stop_at timestamptz;

update research.paper_trades
set initial_stop_loss = coalesce(initial_stop_loss, stop_loss),
    highest_price = coalesce(highest_price, entry_price, entry_trigger),
    time_stop_at = coalesce(time_stop_at, entry_time + interval '3 days')
where initial_stop_loss is null
   or highest_price is null
   or (entry_time is not null and time_stop_at is null);

create table if not exists trading.live_order_gate_state (
    gate_id integer primary key default 1 check (gate_id = 1),
    live_orders_enabled boolean not null default false,
    kill_switch_enabled boolean not null default true,
    max_capital numeric(18,6) not null default 5000,
    max_risk_per_trade numeric(18,6) not null default 50,
    max_daily_loss numeric(18,6) not null default 100,
    max_weekly_loss numeric(18,6) not null default 150,
    max_open_positions integer not null default 1,
    allowed_product_types text[] not null default array['CNC'],
    allowed_order_types text[] not null default array['LIMIT'],
    notes text not null default 'Live-order gate scaffold only. Broker order placement remains disabled until separate explicit approval.',
    updated_at timestamptz not null default now()
);

insert into trading.live_order_gate_state(gate_id)
values (1)
on conflict(gate_id) do nothing;

create index if not exists live_order_gate_state_updated_idx on trading.live_order_gate_state(updated_at desc);
