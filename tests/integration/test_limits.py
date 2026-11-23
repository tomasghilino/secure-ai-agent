"""Rate limits and token budgets in PostgreSQL: atomic, per user, per tenant, over HTTP."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.checkpointer import open_checkpointer
from app.agents.models import ScriptedChatModel, call
from app.agents.service import AgentService
from app.api.ask import get_chat_model
from app.api.dependencies import get_embeddings, get_session
from app.core.config import Settings
from app.main import create_app
from app.rag.embeddings import FakeEmbeddings
from app.security.limits import LimitExceeded, PostgresUsageLimits
from app.tools.authorization import Principal
from scripts.seed_business_data import load_sources, seed

pytestmark = pytest.mark.integration

ALICE = Principal("nsi-amer", "alice", "support_agent")
BOB = Principal("nsi-amer", "bob", "support_agent")
EMEA_ALICE = Principal("nsi-emea", "alice", "support_agent")
NOON = datetime(2026, 11, 15, 12, 0, 30, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def headers(principal: Principal) -> dict[str, str]:
    return {
        "X-Tenant-ID": principal.tenant_id,
        "X-User-ID": principal.user_id,
        "X-User-Role": principal.role,
    }


def limits(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    per_minute: int = 3,
    budget: int = 1000,
) -> PostgresUsageLimits:
    return PostgresUsageLimits(
        sessions, requests_per_minute=per_minute, daily_token_budget=budget, clock=clock
    )


async def test_each_user_has_their_own_rate_limit(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    guard = limits(sessions, Clock(NOON))

    for _ in range(3):
        await guard.count_request(ALICE)
    with pytest.raises(LimitExceeded) as exceeded:
        await guard.count_request(ALICE)
    await guard.count_request(BOB)  # another user: unaffected
    await guard.count_request(EMEA_ALICE)  # same user id, another tenant: unaffected

    assert exceeded.value.retry_after_seconds == 30  # until the next minute starts


async def test_the_limit_resets_with_the_next_window(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(NOON)
    guard = limits(sessions, clock, per_minute=1)
    await guard.count_request(ALICE)

    clock.now = NOON + timedelta(minutes=1)

    await guard.count_request(ALICE)


async def test_concurrent_requests_cannot_overshoot_the_limit(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    guard = limits(sessions, Clock(NOON), per_minute=5)

    results = await asyncio.gather(
        *(guard.count_request(ALICE) for _ in range(20)), return_exceptions=True
    )

    assert sum(result is None for result in results) == 5  # one atomic upsert per request


async def test_the_token_budget_is_per_tenant_and_per_day(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock(NOON)
    guard = limits(sessions, clock, budget=1000)
    await guard.charge_tokens("nsi-amer", 600)
    await guard.ensure_token_budget("nsi-amer")  # 600 < 1000: still allowed
    await guard.charge_tokens("nsi-amer", 600)

    with pytest.raises(LimitExceeded, match="budget"):
        await guard.ensure_token_budget("nsi-amer")
    await guard.ensure_token_budget("nsi-emea")  # another tenant's budget is untouched
    clock.now = NOON + timedelta(days=1)
    await guard.ensure_token_budget("nsi-amer")  # a new UTC day


@pytest.fixture
async def http(
    sessions: async_sessionmaker[AsyncSession], session_with_returns_policies: AsyncSession
) -> AsyncIterator[tuple[httpx.AsyncClient, PostgresUsageLimits]]:
    guard = limits(sessions, Clock(datetime.now(UTC)), per_minute=2, budget=1000)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session_with_returns_policies
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddings()
    app.state.usage_limits = guard
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client, guard


async def test_over_the_limit_the_api_answers_429_with_retry_after(
    http: tuple[httpx.AsyncClient, PostgresUsageLimits],
) -> None:
    client, _ = http
    body = {"query": "return window"}

    statuses = [
        (await client.post("/v1/search", json=body, headers=headers(ALICE))).status_code
        for _ in range(3)
    ]
    limited = await client.post("/v1/search", json=body, headers=headers(ALICE))

    assert statuses == [200, 200, 429]
    assert int(limited.headers["Retry-After"]) >= 1


async def test_a_spent_budget_stops_model_calls(
    http: tuple[httpx.AsyncClient, PostgresUsageLimits],
) -> None:
    client, guard = http
    never = ScriptedChatModel(script=[])  # raises if it is ever called
    client.app.dependency_overrides[get_chat_model] = lambda: never  # type: ignore[attr-defined]
    await guard.charge_tokens("nsi-amer", 1000)

    response = await client.post(
        "/v1/ask", json={"question": "How long is the return window?"}, headers=headers(ALICE)
    )

    assert response.status_code == 429
    assert "budget" in response.json()["detail"]


@pytest.fixture
async def checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(database_url) as saver:
        yield saver


async def test_an_agent_run_is_charged_for_every_model_call(
    sessions: async_sessionmaker[AsyncSession],
    session_with_returns_policies: AsyncSession,
    checkpointer: AsyncPostgresSaver,
) -> None:
    await seed(session_with_returns_policies, load_sources())

    def used(tokens_in: int, tokens_out: int) -> dict[str, int]:
        return {
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        }

    model = ScriptedChatModel(
        script=[
            AIMessage(
                "",
                tool_calls=[call("get_order", order_number="SO-2026-0104")],
                usage_metadata=used(300, 20),  # type: ignore[arg-type]
            ),
            AIMessage("It shipped.", usage_metadata=used(450, 30)),  # type: ignore[arg-type]
        ]
    )
    guard = limits(sessions, Clock(datetime.now(UTC)), per_minute=10, budget=10_000)
    app = create_app()
    app.state.usage_limits = guard
    app.state.agent_service = AgentService(
        sessions=sessions,
        embeddings=FakeEmbeddings(),
        model=model,
        checkpointer=checkpointer,
        settings=Settings(_env_file=None, agent_reference_date=date(2026, 11, 15)),  # type: ignore[call-arg]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/threads", json={"message": "Status of SO-2026-0104?"}, headers=headers(ALICE)
        )

    assert response.json()["tokens"] == 800
    assert await guard.tokens_used_today("nsi-amer") == 800
