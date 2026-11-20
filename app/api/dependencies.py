"""What each request receives through FastAPI's ``Depends``.

Long-lived resources (the connection pool, the embedding client, the queue client) are
created once in the app lifespan and kept on ``app.state``; these functions hand them to
each request. Endpoints declare what they need with the ``*Dep`` aliases, and tests swap
them with ``app.dependency_overrides``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.database.tenancy import tenant_session
from app.ingestion.queue import JobQueue
from app.ingestion.storage import ObjectStorage
from app.rag.embeddings import EmbeddingProvider
from app.tools.authorization import Principal, Role

TENANT_HEADER = "X-Tenant-ID"
USER_HEADER = "X-User-ID"
ROLE_HEADER = "X-User-Role"


def get_tenant_id(
    settings: Annotated[Settings, Depends(get_settings)],
    x_tenant_id: Annotated[str | None, Header(pattern=r"^[a-z0-9-]{1,64}$")] = None,
) -> str:
    """The caller's tenant: every tenant-scoped query takes it from here, never from a body.

    Development stand-in until authentication exists: the tenant comes from the
    X-Tenant-ID header, accepted only in the local and test environments. JWT
    authentication will replace this function with one that reads the tenant from a
    verified token; endpoints do not change.
    """
    if settings.app_env not in ("local", "test"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication is not configured")
    if x_tenant_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"missing {TENANT_HEADER} header")
    return x_tenant_id


def get_principal(
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    x_user_id: Annotated[str | None, Header(pattern=r"^[A-Za-z0-9._@-]{1,64}$")] = None,
    x_user_role: Annotated[Role | None, Header()] = None,
) -> Principal:
    """Who is calling: tenant, user and role (agent endpoints).

    Same development stand-in as the tenant (only accepted in local/test, enforced by
    ``get_tenant_id``): JWT authentication will read all three from a verified token. A
    role sent by the client is exactly what a real system must never trust; it is
    acceptable here only because these headers do not work outside local and test.
    """
    if x_user_id is None or x_user_role is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, f"missing {USER_HEADER} or {ROLE_HEADER} header"
        )
    return Principal(tenant_id, x_user_id, x_user_role)


async def get_session(
    request: Request, tenant_id: Annotated[str, Depends(get_tenant_id)]
) -> AsyncIterator[AsyncSession]:
    """One database session per request, closed once the response is sent.

    It runs under the caller's tenant (Row-Level Security): even a query that forgot its
    tenant filter cannot read or write another tenant's rows.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with tenant_session(factory, tenant_id) as session:
        yield session


def get_embeddings(request: Request) -> EmbeddingProvider:
    embeddings: EmbeddingProvider = request.app.state.embeddings
    return embeddings


def get_storage(request: Request) -> ObjectStorage:
    storage: ObjectStorage = request.app.state.storage
    return storage


def get_job_queue(request: Request) -> JobQueue:
    queue: JobQueue = request.app.state.job_queue
    return queue


SettingsDep = Annotated[Settings, Depends(get_settings)]
TenantDep = Annotated[str, Depends(get_tenant_id)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
EmbeddingsDep = Annotated[EmbeddingProvider, Depends(get_embeddings)]
StorageDep = Annotated[ObjectStorage, Depends(get_storage)]
JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]
