"""Shared test fixtures/markers for the unified suite (legacy + algobot).

``requires_finance_db`` skips tests that need the live finance-db Postgres
(hermes@127.0.0.1:55432/finance_tracker). Deliberately keyed off
FINANCE_DATABASE_URL, NOT DATABASE_URL: several algobot test modules point
DATABASE_URL at throwaway sqlite files at import time, and the legacy DB
tests must not pick that up.
"""
from __future__ import annotations

import os
from functools import lru_cache

import pytest

FINANCE_DATABASE_URL = os.getenv(
    "FINANCE_DATABASE_URL",
    "postgresql://hermes@127.0.0.1:55432/finance_tracker")


@lru_cache(maxsize=1)
def postgres_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(FINANCE_DATABASE_URL, connect_timeout=1):
            return True
    except Exception:
        return False


requires_finance_db = pytest.mark.skipif(
    not postgres_available(),
    reason="finance-db Postgres not reachable (set FINANCE_DATABASE_URL / "
           "start it with ./scripts/start-postgres.sh)")
