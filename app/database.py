import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, declarative_base

SQLALCHEMY_DATABASE_URL = "sqlite:///./carbon_mvp.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Enforce foreign keys on every SQLite connection.

    SQLite ignores FK constraints unless PRAGMA foreign_keys=ON is set per
    connection, so the FKs declared on the models are otherwise decorative and
    orphan rows can be inserted silently. Fires for any SQLite engine in-process
    (app + tests) so referential integrity is consistent everywhere.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
