from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT_ROOT / "migrations" / "016_banknifty_trend_patterns.sql"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")

EXPECTED_CLASSES = {"trend", "range", "spike_channel", "trending_range", "reversal", "chop"}
EXPECTED_DIRECTIONS = {"bullish", "bearish", "neutral", "mixed"}


# --------------------------------------------------------------------------- #
# Static SQL content checks (no DB required)
# --------------------------------------------------------------------------- #
def test_migration_file_exists() -> None:
    assert MIGRATION.exists(), "missing migration 016_banknifty_trend_patterns.sql"


def test_migration_creates_three_research_tables() -> None:
    sql = MIGRATION.read_text().lower()
    for table in (
        "research.banknifty_day_features",
        "research.banknifty_day_classifications",
        "research.banknifty_day_pattern_reports",
    ):
        assert f"create table if not exists {table}" in sql, f"missing create for {table}"


def test_primary_class_check_constraint_covers_six_classes() -> None:
    sql = MIGRATION.read_text()
    # find the primary_class check clause
    m = re.search(r"primary_class[^;]*?check\s*\((.*?)\)", sql, re.IGNORECASE | re.DOTALL)
    assert m, "primary_class check constraint not found"
    clause = m.group(1)
    for cls in EXPECTED_CLASSES:
        assert f"'{cls}'" in clause, f"primary_class check missing {cls}"


def test_direction_check_constraint_present() -> None:
    sql = MIGRATION.read_text()
    m = re.search(r"direction\s+text\s+check\s*\((.*?)\)", sql, re.IGNORECASE | re.DOTALL)
    assert m, "direction check constraint not found"
    clause = m.group(1)
    for d in EXPECTED_DIRECTIONS:
        assert f"'{d}'" in clause, f"direction check missing {d}"


def test_migration_is_idempotent_by_construction() -> None:
    sql = MIGRATION.read_text().lower()
    # every create table/index must be guarded so re-apply cannot fail
    assert sql.count("create table") == sql.count("create table if not exists")
    assert sql.count("create index") == sql.count("create index if not exists")


def test_migration_repairs_columns_with_add_column_if_not_exists() -> None:
    sql = MIGRATION.read_text().lower()
    # CREATE TABLE IF NOT EXISTS cannot fix a partial/draft table; the migration
    # must also ADD COLUMN IF NOT EXISTS to restore any missing column.
    assert "add column if not exists" in sql
    # representative columns from each table must be repairable
    for col in (
        "add column if not exists prev_close",
        "add column if not exists range_vs_adr10",
        "add column if not exists similar_days",
        "add column if not exists markdown",
    ):
        assert col in sql, f"missing repair for {col}"


def test_migration_repairs_constraints_via_pg_constraint_guards() -> None:
    sql = MIGRATION.read_text().lower()
    # check / unique / fk constraints must be re-addable when a draft table lacks
    # them — guarded by pg_constraint existence so re-apply stays idempotent.
    assert sql.count("pg_constraint") >= 4
    assert "banknifty_day_classifications_primary_class_check" in sql
    assert "banknifty_day_classifications_direction_check" in sql
    assert "banknifty_day_features_session_date_key" in sql
    assert "add constraint" in sql


def test_migration_repairs_surrogate_id_columns_and_primary_keys() -> None:
    sql = MIGRATION.read_text().lower()
    # a partial/draft table may have lost its generated id column + PK; the
    # migration must restore each surrogate id (bigserial) before any logic that
    # depends on it (duplicate-collapse orders by report_id; the reports FK targets
    # classifications.classification_id).
    for col in (
        "add column if not exists feature_id bigserial",
        "add column if not exists classification_id bigserial",
        "add column if not exists report_id bigserial",
    ):
        assert col in sql, f"missing surrogate-id repair: {col}"
    # each table's primary key must be re-addable, guarded by a relation-scoped
    # pg_constraint existence check (contype = 'p').
    assert sql.count("contype = 'p'") >= 3, "missing primary-key repair guards"
    for pk in (
        "banknifty_day_features_pkey",
        "banknifty_day_classifications_pkey",
        "banknifty_day_pattern_reports_pkey",
    ):
        assert pk in sql, f"missing primary-key constraint repair: {pk}"


def test_constraint_guards_are_relation_scoped() -> None:
    sql = MIGRATION.read_text().lower()
    # pg_constraint existence checks should be constrained to the target relation,
    # not conname alone, so a same-named constraint on another table can't mask a
    # missing one here.
    assert sql.count("conrelid = 'research.banknifty_day") >= 4


def test_reports_table_is_latest_per_session() -> None:
    sql = MIGRATION.read_text().lower()
    # reports are latest-per-session (files overwrite); the row must be unique on
    # session_date so the report writer can upsert instead of piling up duplicates.
    assert "banknifty_day_pattern_reports_session_date_key" in sql
    m = re.search(
        r"create table if not exists research\.banknifty_day_pattern_reports\s*\((.*?)\);",
        sql, re.IGNORECASE | re.DOTALL,
    )
    assert m, "reports create table not found"
    assert "session_date date not null unique" in m.group(1)


def test_dashboard_ro_grant_is_guarded_by_role_existence() -> None:
    sql = MIGRATION.read_text().lower()
    assert "dashboard_ro" in sql
    assert "pg_roles" in sql and "rolname = 'dashboard_ro'" in sql
    assert "grant select on research.banknifty_day_features to dashboard_ro" in sql


def test_no_live_order_or_execution_artifacts() -> None:
    sql = MIGRATION.read_text().lower()
    assert "live_orders_enabled" not in sql or "live_orders_enabled = false" in sql
    assert "execution_log" not in sql
    assert "place_order" not in sql


# --------------------------------------------------------------------------- #
# Live-DB idempotency + constraint enforcement (skipped if DB unavailable)
# --------------------------------------------------------------------------- #
def _connect():
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(DATABASE_URL, connect_timeout=3)
    except Exception as exc:  # pragma: no cover - depends on local DB
        pytest.skip(f"PostgreSQL unavailable: {exc}")


def test_migration_applies_twice_and_enforces_class_check() -> None:
    import psycopg

    conn = _connect()
    sql = MIGRATION.read_text()
    with conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(sql)  # idempotent
            cur.execute(
                """
                select table_name from information_schema.tables
                where table_schema = 'research'
                  and table_name in ('banknifty_day_features',
                                     'banknifty_day_classifications',
                                     'banknifty_day_pattern_reports')
                """
            )
            assert {r[0] for r in cur.fetchall()} == {
                "banknifty_day_features",
                "banknifty_day_classifications",
                "banknifty_day_pattern_reports",
            }

    # the primary_class check must reject an unknown class
    conn = _connect()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into research.banknifty_day_features(session_date)
                values (date '1999-01-04')
                on conflict (session_date) do nothing
                """
            )
            try:
                cur.execute(
                    """
                    insert into research.banknifty_day_classifications
                        (session_date, primary_class, rule_version)
                    values (date '1999-01-04', 'megatrend', 'test')
                    """
                )
            except psycopg.errors.CheckViolation:
                conn.rollback()
            else:
                conn.rollback()
                raise AssertionError("primary_class check accepted an invalid class")

    # cleanup the probe row
    conn = _connect()
    with conn:
        with conn.cursor() as cur:
            cur.execute("delete from research.banknifty_day_features where session_date = date '1999-01-04'")


def test_migration_repairs_dropped_column_and_constraint() -> None:
    """Re-applying the migration must REPAIR a partial table, not just no-op on an
    existing one: a dropped column and a dropped check constraint are restored."""
    sql = MIGRATION.read_text()

    conn = _connect()
    with conn:
        with conn.cursor() as cur:
            cur.execute(sql)  # ensure baseline exists
            # simulate a partial/draft table: drop a column and a safety constraint
            cur.execute("alter table research.banknifty_day_features drop column if exists range_vs_adr10")
            cur.execute(
                "alter table research.banknifty_day_classifications "
                "drop constraint if exists banknifty_day_classifications_primary_class_check"
            )

    conn = _connect()
    with conn:
        with conn.cursor() as cur:
            cur.execute(sql)  # repair pass
            cur.execute(
                """
                select 1 from information_schema.columns
                where table_schema = 'research'
                  and table_name = 'banknifty_day_features'
                  and column_name = 'range_vs_adr10'
                """
            )
            assert cur.fetchone() is not None, "dropped column was not repaired"
            cur.execute(
                "select 1 from pg_constraint "
                "where conname = 'banknifty_day_classifications_primary_class_check'"
            )
            assert cur.fetchone() is not None, "dropped check constraint was not repaired"


def _has_pk(cur, table: str) -> bool:
    cur.execute(
        "select 1 from pg_constraint "
        "where conrelid = %s::regclass and contype = 'p'",
        (f"research.{table}",),
    )
    return cur.fetchone() is not None


def _has_column(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        select 1 from information_schema.columns
        where table_schema = 'research' and table_name = %s and column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


_ALL_TABLES = (
    "research.banknifty_day_features",
    "research.banknifty_day_classifications",
    "research.banknifty_day_pattern_reports",
)


def test_migration_repairs_dropped_surrogate_id_and_primary_key() -> None:
    """A partial/draft table missing its generated id column + PK must be repaired,
    including feature_id / classification_id / report_id and their primary keys.
    Without this, the duplicate-collapse (orders by report_id) and the reports->
    classifications FK (targets classification_id) would fail before repair.

    Runs inside a transaction that is rolled back, on truncated tables, so the
    shared DB's real rows are untouched and regenerated surrogate keys can't
    collide with existing FK values."""
    sql = MIGRATION.read_text()

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)  # baseline
            cur.execute(f"truncate {', '.join(_ALL_TABLES)} cascade")
            # simulate partial/draft tables that lost their surrogate PKs. Drop the
            # reports FK first so classification_id can be dropped, then the columns
            # (dropping a PK column drops the PK constraint too).
            cur.execute(
                "alter table research.banknifty_day_pattern_reports "
                "drop constraint if exists banknifty_day_pattern_reports_classification_id_fkey"
            )
            cur.execute("alter table research.banknifty_day_pattern_reports drop column if exists report_id")
            cur.execute("alter table research.banknifty_day_classifications drop column if exists classification_id")
            cur.execute("alter table research.banknifty_day_features drop column if exists feature_id")

            assert not _has_column(cur, "banknifty_day_features", "feature_id")
            assert not _has_column(cur, "banknifty_day_classifications", "classification_id")
            assert not _has_column(cur, "banknifty_day_pattern_reports", "report_id")

            cur.execute(sql)  # repair pass — must not raise
            for table, col in (
                ("banknifty_day_features", "feature_id"),
                ("banknifty_day_classifications", "classification_id"),
                ("banknifty_day_pattern_reports", "report_id"),
            ):
                assert _has_column(cur, table, col), f"surrogate id {col} not repaired"
                assert _has_pk(cur, table), f"primary key on {table} not repaired"
    finally:
        conn.rollback()
        conn.close()


def test_migration_remaps_stale_report_classification_id_before_fk() -> None:
    """Re-applying over a partial schema where classifications.classification_id was
    dropped (and regenerated with fresh surrogate ids) while reports.classification_id
    still holds STALE non-null values must NOT fail adding the FK with a
    ForeignKeyViolation. The migration remaps reports.classification_id by session_date
    to the current classification, and nulls anything that still can't be matched.

    Runs inside a rolled-back transaction on truncated tables so the shared DB's real
    rows are untouched and regenerated surrogate keys can't collide with live values."""
    import psycopg

    sql = MIGRATION.read_text()
    matched = "2099-11-30"   # has a classification -> stale id must be remapped
    orphan = "2099-11-29"    # no classification    -> stale id must be nulled

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)  # baseline
            cur.execute(f"truncate {', '.join(_ALL_TABLES)} cascade")
            # seed two feature sessions; classify only the matched one
            cur.execute(
                "insert into research.banknifty_day_features(session_date) "
                "values (%s::date), (%s::date)",
                (matched, orphan),
            )
            cur.execute(
                "insert into research.banknifty_day_classifications"
                "(session_date, primary_class, rule_version) "
                "values (%s::date, 'trend', 'test')",
                (matched,),
            )
            # partial/draft state: drop the reports FK so stale ids can be injected,
            # insert reports carrying non-null classification_id values that match no
            # current classification, then drop the classifications surrogate id so the
            # repair regenerates it (making the injected ids provably stale).
            cur.execute(
                "alter table research.banknifty_day_pattern_reports "
                "drop constraint if exists banknifty_day_pattern_reports_classification_id_fkey"
            )
            cur.execute(
                "insert into research.banknifty_day_pattern_reports"
                "(session_date, classification_id, markdown) "
                "values (%s::date, 987654321, 'matched'), (%s::date, 123456789, 'orphan')",
                (matched, orphan),
            )
            cur.execute("alter table research.banknifty_day_classifications drop column if exists classification_id")

            # repair pass — must NOT raise ForeignKeyViolation
            try:
                cur.execute(sql)
            except psycopg.errors.ForeignKeyViolation as exc:  # pragma: no cover
                raise AssertionError(f"stale classification_id broke the FK add: {exc}")

            # FK is back
            cur.execute(
                "select 1 from pg_constraint "
                "where conname = 'banknifty_day_pattern_reports_classification_id_fkey' "
                "and conrelid = 'research.banknifty_day_pattern_reports'::regclass"
            )
            assert cur.fetchone() is not None, "classification_id FK not re-added"
            # matched report was remapped to the current (regenerated) classification id
            cur.execute(
                "select r.classification_id, c.classification_id "
                "from research.banknifty_day_pattern_reports r "
                "join research.banknifty_day_classifications c on c.session_date = r.session_date "
                "where r.session_date = %s::date",
                (matched,),
            )
            row = cur.fetchone()
            assert row is not None, "matched report row missing"
            assert row[0] is not None and row[0] == row[1], "matched report not remapped to current classification"
            # orphan report (no classification for its session) was nulled
            cur.execute(
                "select classification_id from research.banknifty_day_pattern_reports "
                "where session_date = %s::date",
                (orphan,),
            )
            assert cur.fetchone()[0] is None, "orphan report stale classification_id not nulled"
    finally:
        conn.rollback()
        conn.close()


def test_migration_collapses_duplicate_reports_then_enforces_uniqueness() -> None:
    """If a legacy reports table lost its session_date uniqueness and accumulated
    duplicate rows per session, re-applying must collapse to the most recent row
    (highest report_id) and re-add the unique constraint without failing.

    Runs inside a rolled-back transaction on a truncated reports table so the
    shared DB's real rows are untouched."""
    sql = MIGRATION.read_text()
    probe = "2099-12-31"

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)  # baseline
            cur.execute("truncate research.banknifty_day_pattern_reports cascade")
            # drop uniqueness and inject duplicate rows for one session_date
            cur.execute(
                "alter table research.banknifty_day_pattern_reports "
                "drop constraint if exists banknifty_day_pattern_reports_session_date_key"
            )
            cur.execute(
                "insert into research.banknifty_day_pattern_reports(session_date, markdown) "
                "values (%s::date, 'older'), (%s::date, 'newer')",
                (probe, probe),
            )

            cur.execute(sql)  # repair pass — collapse + re-add unique, must not raise
            cur.execute(
                "select markdown from research.banknifty_day_pattern_reports "
                "where session_date = %s::date",
                (probe,),
            )
            rows = cur.fetchall()
            assert len(rows) == 1, "duplicate reports were not collapsed"
            assert rows[0][0] == "newer", "collapse did not keep the most recent report"
            # uniqueness is now enforced again
            cur.execute(
                "select 1 from pg_constraint "
                "where conname = 'banknifty_day_pattern_reports_session_date_key' "
                "and conrelid = 'research.banknifty_day_pattern_reports'::regclass"
            )
            assert cur.fetchone() is not None, "session_date uniqueness not re-added"
    finally:
        conn.rollback()
        conn.close()
