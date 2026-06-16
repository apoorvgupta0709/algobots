-- BankNifty options paper no-entry (no-trade) decision audit log.
-- Paper/research only. No live broker order capability is created.
--
-- Purpose: make every no-entry tick auditable without emitting noisy Telegram
-- alerts. The quiet cron (`--quiet-no-change`) stays silent on normal no-trade
-- ticks while the top gate decision/reason + key metrics are persisted here for
-- later analysis and the daily no-trade report.
--
-- This table is append-only: every no-entry tick inserts its own row. There is
-- deliberately NO unique/upsert key on the minute bucket — two ticks in the same
-- minute with the same blocker are two distinct audit rows, so the daily report
-- counts every tick rather than collapsing rapid ticks into one.

create schema if not exists research;

create table if not exists research.option_paper_no_entry_decisions (
    decision_id bigserial primary key,
    campaign_id bigint not null references research.option_paper_campaigns(campaign_id),
    trade_date date not null,
    decision_time timestamptz not null,
    -- Minute bucket retained for convenient per-minute grouping in analysis.
    -- NOT an idempotency key: rows are never deduped on it (append-only).
    decision_minute timestamptz not null,
    -- Run context: 'scan' (one-shot) or 'tick' (intraday monitor loop).
    mode text not null default 'scan',
    -- Short machine-friendly blocker code, e.g. 'direction_unconfirmed'.
    blocker text not null,
    -- Full human-readable reason captured from the gate that stopped the entry.
    reason text not null,
    -- Compact key metrics (trade counts, quote staleness, signal/guard raw, etc.).
    metrics jsonb not null default '{}'::jsonb,
    -- Spare structured payload for later analysis.
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

-- Repair: an earlier shape of this migration used a same-minute unique/upsert
-- key on (campaign_id, decision_minute, blocker). The table is now append-only,
-- so that constraint MUST be gone: with it in place, two ticks in the same
-- minute with the same blocker hit a duplicate-key error on the plain INSERT,
-- and record_no_entry_decision swallows exceptions, silently dropping the row.
-- This block drops any unique constraint OR unique index whose key columns are
-- exactly (campaign_id, decision_minute, blocker), regardless of generated name,
-- so re-running the migration over a stale shape repairs it idempotently.
do $$
declare
    rec record;
begin
    -- Drop unique CONSTRAINTS (incl. unique-backed) covering exactly the old
    -- dedupe columns, in any column order.
    for rec in
        select con.conname
        from pg_constraint con
        join pg_class cls on cls.oid = con.conrelid
        join pg_namespace nsp on nsp.oid = cls.relnamespace
        where nsp.nspname = 'research'
          and cls.relname = 'option_paper_no_entry_decisions'
          and con.contype in ('u', 'p')
          and (
              select array_agg(att.attname::text order by att.attname::text)
              from unnest(con.conkey) as k(attnum)
              join pg_attribute att
                on att.attrelid = con.conrelid and att.attnum = k.attnum
          ) = array['blocker', 'campaign_id', 'decision_minute']
    loop
        execute format(
            'alter table research.option_paper_no_entry_decisions drop constraint if exists %I',
            rec.conname
        );
    end loop;

    -- Drop any remaining standalone UNIQUE INDEX covering exactly the old dedupe
    -- columns (an index not backed by a constraint above).
    for rec in
        select idx_cls.relname as indexname
        from pg_index idx
        join pg_class idx_cls on idx_cls.oid = idx.indexrelid
        join pg_class tbl_cls on tbl_cls.oid = idx.indrelid
        join pg_namespace nsp on nsp.oid = tbl_cls.relnamespace
        where nsp.nspname = 'research'
          and tbl_cls.relname = 'option_paper_no_entry_decisions'
          and idx.indisunique
          and (
              select array_agg(att.attname::text order by att.attname::text)
              from unnest(idx.indkey) as k(attnum)
              join pg_attribute att
                on att.attrelid = idx.indrelid and att.attnum = k.attnum
          ) = array['blocker', 'campaign_id', 'decision_minute']
    loop
        execute format(
            'drop index if exists research.%I',
            rec.indexname
        );
    end loop;
end $$;

create index if not exists idx_option_no_entry_decisions_campaign_date
    on research.option_paper_no_entry_decisions (campaign_id, trade_date);

create index if not exists idx_option_no_entry_decisions_date_blocker
    on research.option_paper_no_entry_decisions (trade_date, blocker);

create index if not exists idx_option_no_entry_decisions_time
    on research.option_paper_no_entry_decisions (decision_time desc);
