"""Tenant context for database sessions (Row-Level Security, second isolation layer).

A session opened with ``tenant_session(factory, tenant_id)`` starts every transaction with

    SET LOCAL ROLE northstar_app;                       -- a role subject to RLS policies
    SELECT set_config('app.tenant_id', 'nsi-amer', true);  -- read by the policies

Both last until the end of the transaction only (``LOCAL`` / ``true``), so a pooled
connection never carries one tenant's context into the next request.

Who uses what:
- API requests and the ingestion worker: tenant sessions. A query that forgets its tenant
  filter still cannot see or write another tenant's rows.
- Migrations, seeding and admin scripts: plain sessions as the table owner (no policies).

≈ in Prisma you would run ``SET LOCAL`` inside an interactive ``$transaction`` before the
queries; here an event hook does it for every transaction of the session.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction

APP_ROLE = "northstar_app"  # created by the RLS migration
TENANT_KEY = "tenant_id"


def tenant_session(factory: async_sessionmaker[AsyncSession], tenant_id: str) -> AsyncSession:
    return factory(info={TENANT_KEY: tenant_id})


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(
    session: Session, transaction: SessionTransaction, connection: Connection, *_: Any
) -> None:
    tenant_id = session.info.get(TENANT_KEY)
    if tenant_id is None:
        return
    connection.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": tenant_id}
    )
