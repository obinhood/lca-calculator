"""Database URL configuration — env-driven, Postgres-capable, SQLite default.

Pins the URL normalisation so a provider-supplied `postgres://` env var is rewritten to the
`postgresql://` form SQLAlchemy 2.x requires, and other schemes pass through untouched. The
engine/pool wiring itself is exercised by every other test running on the default SQLite path.
"""
from app.database import _normalise_url, SQLALCHEMY_DATABASE_URL


def test_postgres_scheme_is_rewritten():
    assert _normalise_url("postgres://u:p@host:5432/db") == "postgresql://u:p@host:5432/db"


def test_postgresql_scheme_untouched():
    assert _normalise_url("postgresql://u:p@host/db") == "postgresql://u:p@host/db"


def test_sqlite_scheme_untouched():
    assert _normalise_url("sqlite:///./carbon_mvp.db") == "sqlite:///./carbon_mvp.db"


def test_default_is_sqlite_when_env_unset():
    # The test process sets no DATABASE_URL, so the module default is the local SQLite file.
    assert SQLALCHEMY_DATABASE_URL.startswith("sqlite")
