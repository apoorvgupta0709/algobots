-- Least-privilege role for the BankNifty read-only dashboard.
-- Local pg_hba.conf uses trust auth on loopback, so no password is stored here.

create schema if not exists research;
create schema if not exists market;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'dashboard_ro') then
        create role dashboard_ro login nosuperuser nocreatedb nocreaterole noinherit;
    end if;
end $$;

alter role dashboard_ro set default_transaction_read_only = on;
grant connect on database finance_tracker to dashboard_ro;
grant usage on schema research, market to dashboard_ro;
grant select on all tables in schema research, market to dashboard_ro;
alter default privileges in schema research grant select on tables to dashboard_ro;
alter default privileges in schema market grant select on tables to dashboard_ro;
