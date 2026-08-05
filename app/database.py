import os
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, declarative_base


def _normalise_url(url: str) -> str:
    # Heroku/Render-style "postgres://" is not accepted by SQLAlchemy 1.4+/2.0 — it wants
    # the explicit "postgresql://" driver form. Rewrite it so a provider-supplied env var
    # works unchanged.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


# The database URL is env-driven so the same code runs on SQLite (local/dev/tests) and a
# server database (Postgres) in production without a code change. Default = the local SQLite
# file, so nothing changes for existing dev workflows. alembic reads this same value
# (migrations/env.py imports SQLALCHEMY_DATABASE_URL), so `alembic upgrade` targets whatever
# DATABASE_URL points at.
SQLALCHEMY_DATABASE_URL = _normalise_url(
    os.environ.get("DATABASE_URL", "sqlite:///./carbon_mvp.db"))

_is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    # check_same_thread=False lets the single-file DB be shared across FastAPI's threadpool.
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # Server databases: a real connection pool with pre-ping so a connection dropped by the
    # server (idle timeout, failover) is detected and replaced rather than handed out dead.
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "10")),
        pool_recycle=int(os.environ.get("DB_POOL_RECYCLE_SECONDS", "1800")),
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Enforce foreign keys on every SQLite connection.

    SQLite ignores FK constraints unless PRAGMA foreign_keys=ON is set per
    connection, so the FKs declared on the models are otherwise decorative and
    orphan rows can be inserted silently. Fires for any SQLite engine in-process
    (app + tests) so referential integrity is consistent everywhere. The isinstance
    guard makes this a no-op on Postgres (which enforces FKs natively).
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
