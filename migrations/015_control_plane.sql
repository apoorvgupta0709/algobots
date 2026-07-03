-- Paper-only control plane: dashboard-submitted control requests + engine pause state.
-- The dashboard read path stays SELECT-only via dashboard_ro (migration 009).
-- Control writes go through exactly one INSERT into research.control_requests via
-- the least-privilege dashboard_ctl role below. Local pg_hba.conf uses trust auth on
-- loopback, so this role is defense-in-depth against dashboard bugs, not an auth
-- boundary; the real layers are network access (Tailscale), the dashboard control
-- PIN, and the CHECK constraints here (mode is locked to 'paper' at the DB level).

create schema if not exists research;

create table if not exists research.control_requests (
    request_id      bigint generated always as identity primary key,
    requested_at    timestamptz not null default now(),
    requested_by    text not null,
    engine          text not null check (engine in
                      ('banknifty_options_paper', 'nse_intraday_options_strategy_pack')),
    action_type     text not null check (action_type in
                      ('strategy_toggle', 'force_exit', 'engine_pause', 'engine_resume', 'risk_cap_update')),
    mode            text not null default 'paper' check (mode = 'paper'),
    payload         jsonb not null default '{}'::jsonb,
    status          text not null default 'pending' check (status in
                      ('pending', 'applied', 'rejected', 'expired')),
    processed_at    timestamptz,
    result_message  text
);

create index if not exists control_requests_pending_idx
    on research.control_requests (status, requested_at);

create table if not exists research.control_state (
    engine      text primary key check (engine in
                  ('banknifty_options_paper', 'nse_intraday_options_strategy_pack')),
    paused      boolean not null default false,
    paused_at   timestamptz,
    paused_by   text,
    note        text,
    updated_at  timestamptz not null default now()
);

insert into research.control_state (engine)
values
    ('banknifty_options_paper'),
    ('nse_intraday_options_strategy_pack')
on conflict (engine) do nothing;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'dashboard_ctl') then
        create role dashboard_ctl login nosuperuser nocreatedb nocreaterole noinherit;
    end if;
end $$;

grant connect on database finance_tracker to dashboard_ctl;
grant usage on schema research to dashboard_ctl;
grant insert (requested_by, engine, action_type, mode, payload)
    on research.control_requests to dashboard_ctl;
grant select on research.control_requests, research.control_state to dashboard_ctl;

-- dashboard_ro may not have SELECT on these tables if migration 009 (which sets
-- default privileges) ran after table creation in some environments; grant explicitly.
grant select on research.control_requests, research.control_state to dashboard_ro;
