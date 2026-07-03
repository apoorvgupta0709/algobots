-- BankNifty daywise trend-pattern library.
-- Research / paper-only analytics. No live broker order capability is created.
-- Idempotent AND self-repairing: safe to apply more than once, and re-applying
-- over a partial/draft table restores any missing columns, constraints and
-- indexes (CREATE TABLE IF NOT EXISTS alone cannot repair an existing table).
--
-- Three tables in the research schema:
--   * banknifty_day_features         - one row per session of engineered features
--   * banknifty_day_classifications  - one row per session deterministic label
--   * banknifty_day_pattern_reports  - generated after-market reports (latest per session)

create schema if not exists research;

-- --------------------------------------------------------------------------- --
-- 1. Features
-- --------------------------------------------------------------------------- --
create table if not exists research.banknifty_day_features (
    feature_id bigserial primary key,
    session_date date not null unique,
    underlying text not null default 'BANKNIFTY',
    underlying_symbol text not null default 'NSE:NIFTYBANK-INDEX',
    resolution text not null default '5',
    open numeric(18,6),
    high numeric(18,6),
    low numeric(18,6),
    close numeric(18,6),
    prev_close numeric(18,6),
    gap_pct numeric,
    day_return_pct numeric,
    day_range_pct numeric,
    orb_high numeric(18,6),
    orb_low numeric(18,6),
    orb_range_pct numeric,
    orb_break_direction text not null default 'none',
    orb_hold boolean not null default false,
    close_location numeric,
    vwap_cross_count integer not null default 0,
    vwap_side_pct numeric,
    realized_vol numeric,
    range_vs_adr10 numeric,
    mfe_from_open_pct numeric,
    mae_from_open_pct numeric,
    day_high_time text,
    day_low_time text,
    weighted_positive_breadth_pct numeric,
    weighted_negative_breadth_pct numeric,
    weighted_vwap_confirm_pct numeric,
    breadth_divergence boolean not null default false,
    top_positive_contributors jsonb not null default '[]'::jsonb,
    top_negative_contributors jsonb not null default '[]'::jsonb,
    atm_iv numeric,
    iv_regime text,
    pcr numeric,
    max_pain_distance_pct numeric,
    option_chain_available boolean not null default false,
    candle_count integer not null default 0,
    segments jsonb not null default '[]'::jsonb,
    features jsonb not null default '{}'::jsonb,
    warnings jsonb not null default '[]'::jsonb,
    source text not null default 'banknifty_trend_pattern_engine',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Repair: restore the surrogate primary key FIRST. A partial/draft table may be
-- missing feature_id entirely; everything below (and the classifications FK chain)
-- assumes the generated id + primary key exist. ADD COLUMN ... bigserial recreates
-- the backing sequence and backfills existing rows, then the guard re-adds the PK.
alter table research.banknifty_day_features add column if not exists feature_id bigserial;
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'research.banknifty_day_features'::regclass
          and contype = 'p'
    ) then
        alter table research.banknifty_day_features
            add constraint banknifty_day_features_pkey primary key (feature_id);
    end if;
end $$;

-- Repair: restore any column missing from a partial/draft table.
alter table research.banknifty_day_features add column if not exists session_date date;
alter table research.banknifty_day_features add column if not exists underlying text not null default 'BANKNIFTY';
alter table research.banknifty_day_features add column if not exists underlying_symbol text not null default 'NSE:NIFTYBANK-INDEX';
alter table research.banknifty_day_features add column if not exists resolution text not null default '5';
alter table research.banknifty_day_features add column if not exists open numeric(18,6);
alter table research.banknifty_day_features add column if not exists high numeric(18,6);
alter table research.banknifty_day_features add column if not exists low numeric(18,6);
alter table research.banknifty_day_features add column if not exists close numeric(18,6);
alter table research.banknifty_day_features add column if not exists prev_close numeric(18,6);
alter table research.banknifty_day_features add column if not exists gap_pct numeric;
alter table research.banknifty_day_features add column if not exists day_return_pct numeric;
alter table research.banknifty_day_features add column if not exists day_range_pct numeric;
alter table research.banknifty_day_features add column if not exists orb_high numeric(18,6);
alter table research.banknifty_day_features add column if not exists orb_low numeric(18,6);
alter table research.banknifty_day_features add column if not exists orb_range_pct numeric;
alter table research.banknifty_day_features add column if not exists orb_break_direction text not null default 'none';
alter table research.banknifty_day_features add column if not exists orb_hold boolean not null default false;
alter table research.banknifty_day_features add column if not exists close_location numeric;
alter table research.banknifty_day_features add column if not exists vwap_cross_count integer not null default 0;
alter table research.banknifty_day_features add column if not exists vwap_side_pct numeric;
alter table research.banknifty_day_features add column if not exists realized_vol numeric;
alter table research.banknifty_day_features add column if not exists range_vs_adr10 numeric;
alter table research.banknifty_day_features add column if not exists mfe_from_open_pct numeric;
alter table research.banknifty_day_features add column if not exists mae_from_open_pct numeric;
alter table research.banknifty_day_features add column if not exists day_high_time text;
alter table research.banknifty_day_features add column if not exists day_low_time text;
alter table research.banknifty_day_features add column if not exists weighted_positive_breadth_pct numeric;
alter table research.banknifty_day_features add column if not exists weighted_negative_breadth_pct numeric;
alter table research.banknifty_day_features add column if not exists weighted_vwap_confirm_pct numeric;
alter table research.banknifty_day_features add column if not exists breadth_divergence boolean not null default false;
alter table research.banknifty_day_features add column if not exists top_positive_contributors jsonb not null default '[]'::jsonb;
alter table research.banknifty_day_features add column if not exists top_negative_contributors jsonb not null default '[]'::jsonb;
alter table research.banknifty_day_features add column if not exists atm_iv numeric;
alter table research.banknifty_day_features add column if not exists iv_regime text;
alter table research.banknifty_day_features add column if not exists pcr numeric;
alter table research.banknifty_day_features add column if not exists max_pain_distance_pct numeric;
alter table research.banknifty_day_features add column if not exists option_chain_available boolean not null default false;
alter table research.banknifty_day_features add column if not exists candle_count integer not null default 0;
alter table research.banknifty_day_features add column if not exists segments jsonb not null default '[]'::jsonb;
alter table research.banknifty_day_features add column if not exists features jsonb not null default '{}'::jsonb;
alter table research.banknifty_day_features add column if not exists warnings jsonb not null default '[]'::jsonb;
alter table research.banknifty_day_features add column if not exists source text not null default 'banknifty_trend_pattern_engine';
alter table research.banknifty_day_features add column if not exists created_at timestamptz not null default now();
alter table research.banknifty_day_features add column if not exists updated_at timestamptz not null default now();

-- Repair: restore the session_date uniqueness used as the upsert / FK target.
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'banknifty_day_features_session_date_key'
          and conrelid = 'research.banknifty_day_features'::regclass
    ) then
        alter table research.banknifty_day_features
            add constraint banknifty_day_features_session_date_key unique (session_date);
    end if;
end $$;

create index if not exists banknifty_day_features_date_idx
    on research.banknifty_day_features(session_date desc);

-- --------------------------------------------------------------------------- --
-- 2. Classifications
-- --------------------------------------------------------------------------- --
create table if not exists research.banknifty_day_classifications (
    classification_id bigserial primary key,
    session_date date not null unique
        references research.banknifty_day_features(session_date) on delete cascade,
    primary_class text not null
        check (primary_class in ('trend', 'range', 'spike_channel', 'trending_range', 'reversal', 'chop')),
    direction text check (direction in ('bullish', 'bearish', 'neutral', 'mixed')),
    confidence numeric(5,4),
    rule_version text not null,
    algorithm text not null default 'deterministic_rules',
    secondary_tags text[] not null default '{}',
    explanation jsonb not null default '{}'::jsonb,
    similar_days jsonb not null default '[]'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- Repair: restore the surrogate primary key FIRST. The reports table's FK targets
-- classifications(classification_id), so that column + its primary key must exist
-- before the reports repair runs, even if a partial/draft table dropped them.
alter table research.banknifty_day_classifications add column if not exists classification_id bigserial;
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'research.banknifty_day_classifications'::regclass
          and contype = 'p'
    ) then
        alter table research.banknifty_day_classifications
            add constraint banknifty_day_classifications_pkey primary key (classification_id);
    end if;
end $$;

-- Repair columns.
alter table research.banknifty_day_classifications add column if not exists session_date date;
alter table research.banknifty_day_classifications add column if not exists primary_class text;
alter table research.banknifty_day_classifications add column if not exists direction text;
alter table research.banknifty_day_classifications add column if not exists confidence numeric(5,4);
alter table research.banknifty_day_classifications add column if not exists rule_version text;
alter table research.banknifty_day_classifications add column if not exists algorithm text not null default 'deterministic_rules';
alter table research.banknifty_day_classifications add column if not exists secondary_tags text[] not null default '{}';
alter table research.banknifty_day_classifications add column if not exists explanation jsonb not null default '{}'::jsonb;
alter table research.banknifty_day_classifications add column if not exists similar_days jsonb not null default '[]'::jsonb;
alter table research.banknifty_day_classifications add column if not exists created_at timestamptz not null default now();
alter table research.banknifty_day_classifications add column if not exists updated_at timestamptz not null default now();

-- Repair constraints (unique target, FK, and the safety check constraints that
-- guarantee only the six known classes / four directions are ever stored).
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'banknifty_day_classifications_session_date_key'
          and conrelid = 'research.banknifty_day_classifications'::regclass
    ) then
        alter table research.banknifty_day_classifications
            add constraint banknifty_day_classifications_session_date_key unique (session_date);
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'banknifty_day_classifications_session_date_fkey'
          and conrelid = 'research.banknifty_day_classifications'::regclass
    ) then
        alter table research.banknifty_day_classifications
            add constraint banknifty_day_classifications_session_date_fkey
            foreign key (session_date)
            references research.banknifty_day_features(session_date) on delete cascade;
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'banknifty_day_classifications_primary_class_check'
          and conrelid = 'research.banknifty_day_classifications'::regclass
    ) then
        alter table research.banknifty_day_classifications
            add constraint banknifty_day_classifications_primary_class_check
            check (primary_class in ('trend', 'range', 'spike_channel', 'trending_range', 'reversal', 'chop'));
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'banknifty_day_classifications_direction_check'
          and conrelid = 'research.banknifty_day_classifications'::regclass
    ) then
        alter table research.banknifty_day_classifications
            add constraint banknifty_day_classifications_direction_check
            check (direction in ('bullish', 'bearish', 'neutral', 'mixed'));
    end if;
end $$;

create index if not exists banknifty_day_classifications_class_idx
    on research.banknifty_day_classifications(primary_class, session_date desc);

-- --------------------------------------------------------------------------- --
-- 3. Reports (latest-per-session: the files overwrite, so the row upserts too)
-- --------------------------------------------------------------------------- --
create table if not exists research.banknifty_day_pattern_reports (
    report_id bigserial primary key,
    session_date date not null unique,
    classification_id bigint references research.banknifty_day_classifications(classification_id) on delete set null,
    report_path text,
    markdown text not null,
    generated_at timestamptz not null default now()
);

-- Repair: restore the surrogate primary key FIRST. The duplicate-collapse below
-- orders by report_id to keep the most recent row per session_date, so report_id
-- must exist before that logic runs on a partial/draft table that dropped it.
alter table research.banknifty_day_pattern_reports add column if not exists report_id bigserial;
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'research.banknifty_day_pattern_reports'::regclass
          and contype = 'p'
    ) then
        alter table research.banknifty_day_pattern_reports
            add constraint banknifty_day_pattern_reports_pkey primary key (report_id);
    end if;
end $$;

-- Repair columns.
alter table research.banknifty_day_pattern_reports add column if not exists session_date date;
alter table research.banknifty_day_pattern_reports add column if not exists classification_id bigint;
alter table research.banknifty_day_pattern_reports add column if not exists report_path text;
alter table research.banknifty_day_pattern_reports add column if not exists markdown text;
alter table research.banknifty_day_pattern_reports add column if not exists generated_at timestamptz not null default now();

-- Repair the latest-per-session uniqueness + the classification FK. If a legacy
-- table accumulated multiple rows per session_date, collapse to the most recent
-- before enforcing uniqueness so the ADD CONSTRAINT cannot fail.
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'banknifty_day_pattern_reports_session_date_key'
          and conrelid = 'research.banknifty_day_pattern_reports'::regclass
    ) then
        delete from research.banknifty_day_pattern_reports r
        using research.banknifty_day_pattern_reports newer
        where r.session_date = newer.session_date
          and r.report_id < newer.report_id;
        alter table research.banknifty_day_pattern_reports
            add constraint banknifty_day_pattern_reports_session_date_key unique (session_date);
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'banknifty_day_pattern_reports_classification_id_fkey'
          and conrelid = 'research.banknifty_day_pattern_reports'::regclass
    ) then
        -- Repair stale classification_id BEFORE re-adding the FK. On a partial/draft
        -- schema where classifications.classification_id was dropped and regenerated
        -- (bigserial reassigns new surrogate ids), reports.classification_id can hold
        -- stale non-null values that match no current classification — adding the FK
        -- then fails with ForeignKeyViolation. First remap by session_date to the
        -- authoritative current classification, then null anything still unmatched
        -- (the FK is on delete set null, so null is the safe repaired state).
        update research.banknifty_day_pattern_reports r
        set classification_id = c.classification_id
        from research.banknifty_day_classifications c
        where r.session_date = c.session_date
          and r.classification_id is distinct from c.classification_id;

        update research.banknifty_day_pattern_reports r
        set classification_id = null
        where r.classification_id is not null
          and not exists (
              select 1 from research.banknifty_day_classifications c
              where c.classification_id = r.classification_id
          );

        alter table research.banknifty_day_pattern_reports
            add constraint banknifty_day_pattern_reports_classification_id_fkey
            foreign key (classification_id)
            references research.banknifty_day_classifications(classification_id) on delete set null;
    end if;
end $$;

create index if not exists banknifty_day_pattern_reports_date_idx
    on research.banknifty_day_pattern_reports(session_date desc, generated_at desc);

-- --------------------------------------------------------------------------- --
-- Least-privilege read access for the dashboard role (created in migration 009).
-- --------------------------------------------------------------------------- --
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'dashboard_ro') then
        grant select on research.banknifty_day_features to dashboard_ro;
        grant select on research.banknifty_day_classifications to dashboard_ro;
        grant select on research.banknifty_day_pattern_reports to dashboard_ro;
    end if;
end $$;
