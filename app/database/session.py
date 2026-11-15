"""Async database access.

One engine per process (it owns the connection pool) and one session per unit of work
(a request, a queue message, a script run). The ``postgresql+psycopg://`` URL serves
both this async engine and Alembic's sync engine.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping replaces connections the server closed (restarts, idle timeouts).
    return create_async_engine(database_url, pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit without another query.
    return async_sessionmaker(engine, expire_on_commit=False)
