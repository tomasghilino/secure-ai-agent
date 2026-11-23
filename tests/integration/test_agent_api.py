"""Agent endpoints over HTTP with the offline rule-based model (LLM_PROVIDER=fake)."""

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.checkpointer import open_checkpointer
from app.agents.models import FakeAgentChatModel
from app.agents.service import AgentService
from app.core.config import Settings
from app.main import create_app
from app.rag.embeddings import FakeEmbeddings
from app.security.limits import Unlimited
from app.security.tokens import TokenVerifier
from scripts.dev_token import DEV_AUDIENCE, DEV_ISSUER, generate_signing_key, mint, public_jwks
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
async def app(
    sessions: async_sessionmaker[AsyncSession],
    session_with_returns_policies: AsyncSession,
    checkpointer: AsyncPostgresSaver,
) -> FastAPI:
    await seed(session_with_returns_policies, load_sources())
    app = create_app()
    app.state.usage_limits = Unlimited()
    app.state.agent_service = AgentService(
        sessions=sessions,
        embeddings=FakeEmbeddings(),
        model=FakeAgentChatModel(),
        checkpointer=checkpointer,
        settings=Settings(_env_file=None, agent_reference_date=date(2026, 11, 15)),  # type: ignore[call-arg]
    )
    return app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
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


async def test_in_jwt_mode_the_identity_comes_only_from_the_token(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.core.config import get_settings

    key = generate_signing_key()
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(public_jwks(key)), encoding="utf-8")
    for name, value in {
        "AUTH_MODE": "jwt",
        "AUTH_JWT_ISSUER": DEV_ISSUER,
        "AUTH_JWT_AUDIENCE": DEV_AUDIENCE,
        "AUTH_JWKS_PATH": str(jwks),
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    app.state.token_verifier = TokenVerifier.from_settings(get_settings())
    support = mint(key, subject="agent-7", tenant="nsi-amer", roles=["support_agent"])

    spoofed = await client.post("/v1/agent/threads", json=RETURN_REQUEST, headers=SUPERVISOR)
    started = await client.post(
        "/v1/agent/threads", json=RETURN_REQUEST, headers={"Authorization": f"Bearer {support}"}
    )
    thread = started.json()["thread_id"]
    as_owner = await client.get(
        f"/v1/agent/threads/{thread}", headers={"Authorization": f"Bearer {support}"}
    )

    assert spoofed.status_code == 401  # identity headers mean nothing with JWT on
    assert started.json()["status"] == "awaiting_approval"
    assert as_owner.json()["created_by"] == "agent-7"  # the token's subject
