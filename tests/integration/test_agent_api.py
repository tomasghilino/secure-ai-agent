"""Agent endpoints over HTTP with the offline rule-based model (LLM_PROVIDER=fake)."""

from collections.abc import AsyncIterator
from datetime import date

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.checkpointer import open_checkpointer
from app.agents.models import FakeAgentChatModel
from app.agents.service import AgentService
from app.core.config import Settings
from app.main import create_app
from app.rag.embeddings import FakeEmbeddings
from scripts.seed_business_data import load_sources, seed

pytestmark = pytest.mark.integration


def as_user(user: str, role: str, tenant: str = "nsi-amer") -> dict[str, str]:
    return {"X-Tenant-ID": tenant, "X-User-ID": user, "X-User-Role": role}


SUPPORT = as_user("agent-1", "support_agent")
SUPERVISOR = as_user("sup-1", "supervisor")
RETURN_REQUEST = {
    "message": "Please create a return authorization for SO-2026-0101: wrong pump size."
}


@pytest.fixture
async def checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(database_url) as saver:
        yield saver


@pytest.fixture
async def client(
    sessions: async_sessionmaker[AsyncSession],
    session_with_returns_policies: AsyncSession,
    checkpointer: AsyncPostgresSaver,
) -> AsyncIterator[httpx.AsyncClient]:
    await seed(session_with_returns_policies, load_sources())
    app = create_app()
    app.state.agent_service = AgentService(
        sessions=sessions,
        embeddings=FakeEmbeddings(),
        model=FakeAgentChatModel(),
        checkpointer=checkpointer,
        settings=Settings(_env_file=None, agent_reference_date=date(2026, 11, 15)),  # type: ignore[call-arg]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_a_question_runs_the_tools_and_returns_the_trace(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/agent/threads", json={"message": "Status of SO-2026-0104?"}, headers=SUPPORT
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "completed"
    assert body["tool_calls"] == [
        {"tool": "get_order", "arguments": {"order_number": "SO-2026-0104"}, "status": "ok"}
    ]


async def test_identity_headers_are_required(client: httpx.AsyncClient) -> None:
    missing = await client.post(
        "/v1/agent/threads", json={"message": "hi"}, headers={"X-Tenant-ID": "nsi-amer"}
    )
    bad_role = await client.post(
        "/v1/agent/threads", json={"message": "hi"}, headers=as_user("u-1", "admin")
    )

    assert missing.status_code == 401
    assert bad_role.status_code == 422


async def test_the_approval_flow_over_http(client: httpx.AsyncClient) -> None:
    started = (await client.post("/v1/agent/threads", json=RETURN_REQUEST, headers=SUPPORT)).json()
    thread = started["thread_id"]
    assert started["status"] == "awaiting_approval"
    assert started["pending_action"]["facts"]["days_since_delivery"] == 12

    queue_for_support = await client.get("/v1/agent/approvals", headers=SUPPORT)
    queue_for_supervisor = await client.get("/v1/agent/approvals", headers=SUPERVISOR)
    self_approval = await client.post(
        f"/v1/agent/threads/{thread}/decision", json={"approve": True}, headers=SUPPORT
    )
    approved = await client.post(
        f"/v1/agent/threads/{thread}/decision",
        json={"approve": True, "comment": "Checked: within 30 days."},
        headers=SUPERVISOR,
    )
    again = await client.post(
        f"/v1/agent/threads/{thread}/decision", json={"approve": True}, headers=SUPERVISOR
    )

    assert queue_for_support.status_code == 403
    assert [t["thread_id"] for t in queue_for_supervisor.json()] == [thread]
    assert self_approval.status_code == 403
    assert approved.json()["status"] == "completed"
    assert "RMA-SO-2026-0101" in approved.json()["answer"]
    assert again.status_code == 409


async def test_the_audit_log_is_for_supervisors_and_the_tenant_only(
    client: httpx.AsyncClient,
) -> None:
    started = (await client.post("/v1/agent/threads", json=RETURN_REQUEST, headers=SUPPORT)).json()
    thread = started["thread_id"]

    events = await client.get(f"/v1/audit-events?thread_id={thread}", headers=SUPERVISOR)
    as_support = await client.get("/v1/audit-events", headers=SUPPORT)
    other_tenant = await client.get(
        f"/v1/agent/threads/{thread}", headers=as_user("sup-9", "supervisor", "nsi-emea")
    )

    assert [(e["event_type"], e["outcome"]) for e in events.json()] == [
        ("action_proposed", "pending_approval"),
        ("tool_call", "ok"),
    ]
    assert as_support.status_code == 403
    assert other_tenant.status_code == 404


async def test_the_kill_switch_disables_the_agent(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AGENT_ENABLED", "false")
    get_settings.cache_clear()

    response = await client.post("/v1/agent/threads", json={"message": "hi"}, headers=SUPPORT)

    assert response.status_code == 503
