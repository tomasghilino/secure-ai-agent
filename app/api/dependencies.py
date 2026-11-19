"""What each request receives through FastAPI's ``Depends`` (≈ NestJS providers and guards).

Long-lived resources (the connection pool, the embedding client) are created once in the
app lifespan and kept on ``app.state``; these functions hand them to each request.
Endpoints declare what they need with the ``*Dep`` aliases, and tests swap them with
``app.dependency_overrides``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.ingestion.queue import JobQueue
from app.ingestion.storage import ObjectStorage
from app.rag.embeddings import EmbeddingProvider

TENANT_HEADER = "X-Tenant-ID"


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One database session per request, closed once the response is sent."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
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


def get_tenant_id(
    settings: Annotated[Settings, Depends(get_settings)],
    x_tenant_id: Annotated[str | None, Header(pattern=r"^[a-z0-9-]{1,64}$")] = None,
) -> str:
    """The caller's tenant: every tenant-scoped query takes it from here, never from a body.

    Development stand-in until authentication exists (Day 4): the tenant comes from the
    X-Tenant-ID header, accepted only in the local and test environments. Day 4 replaces
    this function with one that reads the tenant from a verified JWT; endpoints do not
    change.
    """
    if settings.app_env not in ("local", "test"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication is not configured")
    if x_tenant_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"missing {TENANT_HEADER} header")
    return x_tenant_id


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
EmbeddingsDep = Annotated[EmbeddingProvider, Depends(get_embeddings)]
StorageDep = Annotated[ObjectStorage, Depends(get_storage)]
JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]
TenantDep = Annotated[str, Depends(get_tenant_id)]
