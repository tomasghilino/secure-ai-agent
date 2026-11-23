"""What each request receives through FastAPI's ``Depends``.

Long-lived resources (the connection pool, the embedding client, the queue client) are
created once in the app lifespan and kept on ``app.state``; these functions hand them to
each request. Endpoints declare what they need with the ``*Dep`` aliases, and tests swap
them with ``app.dependency_overrides``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Collection
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.database.tenancy import tenant_session
from app.ingestion.queue import JobQueue
from app.ingestion.storage import ObjectStorage
from app.rag.embeddings import EmbeddingProvider
from app.security.limits import LimitExceeded, UsageLimits
from app.security.tokens import AccessDenied, InvalidToken, TokenVerifier
from app.tools.authorization import Principal, Role

log = get_logger(__name__)

TENANT_HEADER = "X-Tenant-ID"
USER_HEADER = "X-User-ID"
ROLE_HEADER = "X-User-Role"
_UNAUTHENTICATED = {"WWW-Authenticate": 'Bearer error="invalid_token"'}

bearer_scheme = HTTPBearer(auto_error=False, description="JWT from the identity provider")


def get_principal(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
    x_tenant_id: Annotated[str | None, Header(pattern=r"^[a-z0-9-]{1,64}$")] = None,
    x_user_id: Annotated[str | None, Header(pattern=r"^[A-Za-z0-9._@-]{1,64}$")] = None,
    x_user_role: Annotated[Role | None, Header()] = None,
) -> Principal:
    """Who is calling: tenant, user and role. Every /v1 endpoint starts here (ADR-0009).

    - ``AUTH_MODE=jwt``: a bearer token verified against the provider's keys; the identity
      comes only from its claims.
    - ``AUTH_MODE=dev_headers`` (local and test only, enforced by ``Settings`` at startup
      and again here): the X-Tenant-ID / X-User-ID / X-User-Role headers. A role chosen
      by the client is exactly what a real system must never trust.

    A plain ``def``: FastAPI runs it in a worker thread, so fetching the provider's keys
    on a cache miss never blocks the event loop.
    """
    if settings.auth_mode == "jwt":
        principal = _from_token(request, credentials)
    else:
        principal = _from_dev_headers(settings, x_tenant_id, x_user_id, x_user_role)
    request.state.principal = principal  # for the access log (app/api/middleware.py)
    return principal


def _from_token(request: Request, credentials: HTTPAuthorizationCredentials | None) -> Principal:
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token", headers=_UNAUTHENTICATED
        )
    verifier: TokenVerifier = request.app.state.token_verifier
    try:
        return verifier.verify(credentials.credentials)
    except InvalidToken as exc:
        # The reason goes to the log, not to the caller: no oracle for forging tokens.
        log.info("auth.rejected", reason=str(exc))
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or expired token", headers=_UNAUTHENTICATED
        ) from exc
    except AccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


def _from_dev_headers(
    settings: Settings, tenant: str | None, user: str | None, role: Role | None
) -> Principal:
    if settings.app_env not in ("local", "test"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication is not configured")
    if tenant is None or user is None or role is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"missing {TENANT_HEADER}, {USER_HEADER} or {ROLE_HEADER} header",
        )
    return Principal(tenant, user, role)


def get_tenant_id(principal: Annotated[Principal, Depends(get_principal)]) -> str:
    """The caller's tenant: every tenant-scoped query takes it from here, never from a body."""
    return principal.tenant_id


def require_role(allowed: Collection[Role], action: str) -> Callable[..., Principal]:
    """A dependency that lets only ``allowed`` roles through (403 otherwise)."""

    def check(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role {principal.role} cannot {action}")
        return principal

    return check


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


def get_usage_limits(request: Request) -> UsageLimits:
    limits: UsageLimits = request.app.state.usage_limits
    return limits


def _too_many(exc: LimitExceeded) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        str(exc),
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


async def enforce_rate_limit(
    principal: Annotated[Principal, Depends(get_principal)],
    limits: Annotated[UsageLimits, Depends(get_usage_limits)],
) -> None:
    """Requests per user per minute; applied to every /v1 router in ``app.main``."""
    try:
        await limits.count_request(principal)
    except LimitExceeded as exc:
        raise _too_many(exc) from exc


async def enforce_token_budget(
    principal: Annotated[Principal, Depends(get_principal)],
    limits: Annotated[UsageLimits, Depends(get_usage_limits)],
) -> None:
    """Refuse model-backed requests once the tenant's daily token budget is spent."""
    try:
        await limits.ensure_token_budget(principal.tenant_id)
    except LimitExceeded as exc:
        raise _too_many(exc) from exc


SettingsDep = Annotated[Settings, Depends(get_settings)]
TenantDep = Annotated[str, Depends(get_tenant_id)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
EmbeddingsDep = Annotated[EmbeddingProvider, Depends(get_embeddings)]
StorageDep = Annotated[ObjectStorage, Depends(get_storage)]
JobQueueDep = Annotated[JobQueue, Depends(get_job_queue)]
UsageLimitsDep = Annotated[UsageLimits, Depends(get_usage_limits)]
RateLimited = Depends(enforce_rate_limit)
WithinTokenBudget = Depends(enforce_token_budget)
