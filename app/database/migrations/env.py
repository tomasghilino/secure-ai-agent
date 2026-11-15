"""Alembic environment: applies migrations to the database in DATABASE_URL.

Migrations run with a synchronous engine: psycopg 3 serves sync and async engines from
the same URL, and sync code sidesteps psycopg's async limitation on Windows (ADR-0002).
Tests can pass an open connection in ``config.attributes["connection"]`` to migrate a
throwaway database instead.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from app.core.config import get_settings
from app.database.models import Base

config = context.config
if config.config_file_name is not None and "connection" not in config.attributes:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Print the SQL instead of running it: ``alembic upgrade head --sql``."""
    context.configure(
        url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return
    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with engine.connect() as new_connection:
        _run_migrations(new_connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
