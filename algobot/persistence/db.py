"""Database engine/session factory. DATABASE_URL env selects Postgres or SQLite."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from algobot.core.config import settings
from algobot.persistence.schema import Base


@lru_cache(maxsize=1)
def get_engine():
    url = settings()["database_url"]
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return create_engine(url, **kwargs)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables if absent. Idempotent; every service calls it on boot."""
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Session:
    """Transactional scope: commit on success, rollback on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
