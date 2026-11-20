"""Durable agent state: LangGraph's PostgreSQL checkpointer.

After every step LangGraph saves the graph state (messages, pending action) under the
run's ``thread_id``. That is what lets a run pause for approval and resume later, in
another request or another process, and what makes a conversation multi-turn.

The tables live in their own schema (``agent_checkpoints``, created by a migration) so
Alembic, which only manages ``public``, never mistakes them for tables to drop. They are
created by ``setup()``, idempotently, at startup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import make_url

CHECKPOINT_SCHEMA = "agent_checkpoints"


def checkpoint_conninfo(database_url: str) -> str:
    """The SQLAlchemy URL (``postgresql+psycopg://``) as a plain libpq URL."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    kwargs: dict[str, Any] = {
        # Required by the saver: each statement commits on its own, rows come as dicts.
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        "options": f"-c search_path={CHECKPOINT_SCHEMA}",
    }
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        checkpoint_conninfo(database_url), kwargs=kwargs, min_size=1, max_size=5, open=False
    )
    async with pool:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver
