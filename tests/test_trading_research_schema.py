from __future__ import annotations

from pathlib import Path

import psycopg

from tests.conftest import FINANCE_DATABASE_URL, requires_finance_db

pytestmark = requires_finance_db

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT_ROOT / "migrations" / "001_trading_research_schemas.sql"
CONTROL_PLANE_MIGRATION = PROJECT_ROOT / "migrations" / "015_control_plane.sql"
# FINANCE_DATABASE_URL, not DATABASE_URL: algobot test modules repoint
# DATABASE_URL at sqlite, which psycopg cannot connect to.
DATABASE_URL = FINANCE_DATABASE_URL


def test_trading_research_migration_creates_required_schemas_tables_and_views() -> None:
    assert MIGRATION.exists(), "missing Phase 1 trading/research schema migration"

    sql = MIGRATION.read_text()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()

            expected_tables = {
                ("knowledge", "sources"),
                ("knowledge", "chunks"),
                ("knowledge", "concepts"),
                ("knowledge", "rules"),
                ("knowledge", "playbooks"),
                ("research", "hypotheses"),
                ("research", "strategy_versions"),
                ("research", "backtest_runs"),
                ("research", "backtest_trades"),
                ("research", "factor_snapshots"),
                ("research", "model_outputs"),
                ("trading", "positions_snapshots"),
                ("trading", "orderbook_snapshots"),
                ("trading", "holdings_snapshots"),
                ("trading", "funds_snapshots"),
                ("trading", "trade_ideas"),
                ("trading", "approvals"),
                ("trading", "execution_log"),
            }
            cur.execute(
                """
                select table_schema, table_name
                from information_schema.tables
                where table_schema in ('knowledge', 'research', 'trading')
                  and table_type = 'BASE TABLE'
                """
            )
            actual_tables = set(cur.fetchall())
            assert expected_tables <= actual_tables

            expected_views = {
                ("research", "latest_strategy_metrics"),
                ("trading", "open_trade_ideas"),
            }
            cur.execute(
                """
                select table_schema, table_name
                from information_schema.views
                where table_schema in ('research', 'trading')
                """
            )
            actual_views = set(cur.fetchall())
            assert expected_views <= actual_views


def test_execution_log_requires_explicit_approval_before_live_order_record() -> None:
    sql = MIGRATION.read_text()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()

            cur.execute(
                """
                insert into trading.trade_ideas(symbol, side, quantity, order_type, product_type, validity, rationale, status)
                values ('NSE:TVSMOTOR-EQ', 'BUY', 1, 'MARKET', 'CNC', 'DAY', 'schema guardrail test', 'approved')
                returning idea_id
                """
            )
            idea_id = cur.fetchone()[0]

            try:
                cur.execute(
                    """
                    insert into trading.execution_log(idea_id, symbol, side, quantity, order_type, product_type, validity, action, api_status, raw)
                    values (%s, 'NSE:TVSMOTOR-EQ', 'BUY', 1, 'MARKET', 'CNC', 'DAY', 'place_order', 'blocked', '{}'::jsonb)
                    """,
                    (idea_id,),
                )
            except psycopg.IntegrityError:
                conn.rollback()
            else:
                raise AssertionError("execution log accepted a live order record without approval_id")


def test_approvals_store_confirmation_terms_for_auditable_live_order_scope() -> None:
    sql = MIGRATION.read_text()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()

            cur.execute(
                """
                insert into trading.trade_ideas(symbol, side, quantity, order_type, product_type, validity, rationale, status)
                values ('NSE:SBIN-EQ', 'BUY', 1, 'LIMIT', 'CNC', 'DAY', 'approval schema test', 'approved')
                returning idea_id
                """
            )
            idea_id = cur.fetchone()[0]
            cur.execute(
                """
                insert into trading.approvals(
                    idea_id, approved_by, confirmation_text, symbol, side, quantity,
                    order_type, price, product_type, validity, max_loss_amount, status
                )
                values (
                    %s, 'apoorv',
                    'Approve NSE:SBIN-EQ BUY LIMIT quantity 1 price 600 CNC DAY max loss 100',
                    'NSE:SBIN-EQ', 'BUY', 1, 'LIMIT', 600, 'CNC', 'DAY', 100, 'approved'
                )
                returning approval_id
                """,
                (idea_id,),
            )
            approval_id = cur.fetchone()[0]

            cur.execute(
                """
                insert into trading.execution_log(
                    approval_id, idea_id, symbol, side, quantity, order_type, price,
                    product_type, validity, action, api_status, raw
                )
                values (%s, %s, 'NSE:SBIN-EQ', 'BUY', 1, 'LIMIT', 600, 'CNC', 'DAY', 'place_order', 'dry_run_recorded', '{}'::jsonb)
                returning execution_id
                """,
                (approval_id, idea_id),
            )
            assert cur.fetchone()[0] is not None
            conn.rollback()


def test_control_plane_migration_is_idempotent_and_locks_mode_to_paper() -> None:
    assert CONTROL_PLANE_MIGRATION.exists(), "missing control plane migration"

    sql = CONTROL_PLANE_MIGRATION.read_text()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # idempotent: applying twice must not error
            cur.execute(sql)
            cur.execute(sql)
            conn.commit()

            cur.execute(
                """
                select table_name
                from information_schema.tables
                where table_schema = 'research'
                  and table_name in ('control_requests', 'control_state')
                """
            )
            assert {row[0] for row in cur.fetchall()} == {"control_requests", "control_state"}

            cur.execute("select engine, paused from research.control_state order by engine")
            state = dict(cur.fetchall())
            assert set(state) == {"banknifty_options_paper", "nse_intraday_options_strategy_pack"}

            # mode is locked to 'paper' at the DB level
            try:
                cur.execute(
                    """
                    insert into research.control_requests (requested_by, engine, action_type, mode)
                    values ('schema-test', 'banknifty_options_paper', 'engine_pause', 'live')
                    """
                )
            except psycopg.IntegrityError:
                conn.rollback()
            else:
                raise AssertionError("control_requests accepted a non-paper mode")


def test_control_plane_role_is_insert_and_select_only() -> None:
    sql = CONTROL_PLANE_MIGRATION.read_text()
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()

    # Local pg_hba uses trust auth on loopback, so we can connect as the control role.
    ctl_url = DATABASE_URL.replace("hermes@", "dashboard_ctl@")
    with psycopg.connect(ctl_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into research.control_requests (requested_by, engine, action_type, mode, payload)
                values ('schema-test', 'banknifty_options_paper', 'engine_pause', 'paper', '{}'::jsonb)
                """
            )
            conn.rollback()

            try:
                cur.execute(
                    "update research.control_requests set status = 'applied' where requested_by = 'schema-test'"
                )
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
            else:
                raise AssertionError("dashboard_ctl was able to UPDATE control_requests")

            try:
                cur.execute("delete from research.control_requests where requested_by = 'schema-test'")
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
            else:
                raise AssertionError("dashboard_ctl was able to DELETE from control_requests")

            try:
                cur.execute(
                    """
                    insert into research.control_requests (requested_by, engine, action_type, mode, status)
                    values ('schema-test', 'banknifty_options_paper', 'engine_pause', 'paper', 'applied')
                    """
                )
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
            else:
                raise AssertionError("dashboard_ctl was able to INSERT a non-granted column (status)")
