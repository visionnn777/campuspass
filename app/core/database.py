"""Database engine and per-request sessions (spec §2, §8 Rule 4).

* One SQLAlchemy Session per request, never shared between threads.
* Every connection sets `lock_timeout` and `statement_timeout`, so a stuck
  transaction can never make every other request wait forever (§8 Rule 4).
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def build_engine(url: str) -> Engine:
    settings = get_settings()
    return create_engine(
        url,
        pool_pre_ping=True,  # replaces dead connections instead of failing a request
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        connect_args={
            # libpq "options": applied to every new connection in the pool.
            "options": (
                f"-c lock_timeout={settings.db_lock_timeout_ms} "
                f"-c statement_timeout={settings.db_statement_timeout_ms} "
                "-c timezone=UTC"
            ),
        },
    )


engine: Engine = build_engine(get_settings().database_url)

# expire_on_commit=False: objects keep their loaded values after commit, so a service
# can build its response after the transaction (and its locks) has ended.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always rolled back on error."""
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()  # also rolls back anything left open
