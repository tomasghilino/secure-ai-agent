"""Integration tests against the local PostgreSQL from docker compose (`make up`).

They run in a separate database, created from scratch and migrated with Alembic at the
start of the run: development data is never touched, and the migrations get tested too.
Skipped when PostgreSQL is not reachable.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, make_url, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.database.session import build_engine, build_session_factory

TEST_DATABASE = "northstar_test"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config(connection: Connection) -> Config:
    config = Config(ALEMBIC_INI)
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """URL of a freshly created and fully migrated test database."""
    server = make_url(Settings().database_url)
    admin = create_engine(server.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
            connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"PostgreSQL not available (run `make up`): {exc}")

    url = server.set(database=TEST_DATABASE).render_as_string(hide_password=False)
    migrator = create_engine(url)
    with migrator.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    migrator.dispose()

    yield url

    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory on empty tables: every test starts from scratch."""
    engine = build_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE documents CASCADE"))
    yield build_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def session(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:
        yield session
