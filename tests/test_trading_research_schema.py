from __future__ import annotations

import os
from pathlib import Path

import psycopg


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT_ROOT / "migrations" / "001_trading_research_schemas.sql"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://hermes@127.0.0.1:55432/finance_tracker")


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
