"""POST /v1/search over HTTP, against a real PostgreSQL with fake embeddings."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_embeddings, get_session
from app.main import create_app
from app.rag.embeddings import FakeEmbeddings
from tests.integration.conftest import RETURNS_EMEA, RETURNS_V3_0

pytestmark = pytest.mark.integration

QUESTION = {"query": "When must a return request be submitted?"}
AMER = {"X-Tenant-ID": "nsi-amer"}


@pytest.fixture
async def client(
    session_with_returns_policies: AsyncSession,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    # No lifespan runs here, so the app creates no pool or embedding client of its own:
    # the test database session and the fake embeddings are injected instead.
    app.dependency_overrides[get_session] = lambda: session_with_returns_policies
    app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_returns_the_closest_chunks_with_their_citation(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/search", json=QUESTION, headers=AMER)

    assert response.status_code == 200
    body = response.json()
    assert body["embedding_model"] == "fake-feature-hashing-1024"
    assert len(body["results"]) == 5
    top = body["results"][0]
    assert (top["doc_key"], top["version"], top["page_number"]) == ("NSI-POL-RET-003", "3.0", 1)
    assert top["section_path"] == ["Returns and Refunds Policy", "3. Return Eligibility"]


@pytest.mark.parametrize(
    ("tenant_id", "expected"), [("nsi-amer", RETURNS_V3_0), ("nsi-emea", RETURNS_EMEA)]
)
async def test_the_caller_only_sees_the_active_version_of_its_tenant(
    client: httpx.AsyncClient, tenant_id: str, expected: tuple[str, str]
) -> None:
    response = await client.post(
        "/v1/search", json={**QUESTION, "limit": 20}, headers={"X-Tenant-ID": tenant_id}
    )

    results = response.json()["results"]
    assert results
    assert {(r["doc_key"], r["version"]) for r in results} == {expected}


async def test_a_request_body_cannot_choose_the_tenant(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/search", json={**QUESTION, "tenant_id": "nsi-emea"}, headers=AMER
    )

    assert response.status_code == 422


async def test_a_request_without_a_tenant_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/search", json=QUESTION)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [{"query": "   "}, {"query": "returns", "limit": 0}, {"query": "returns", "limit": 21}],
)
async def test_invalid_requests_are_rejected(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    response = await client.post("/v1/search", json=body, headers=AMER)

    assert response.status_code == 422


async def test_a_malformed_tenant_header_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/search", json=QUESTION, headers={"X-Tenant-ID": "NSI AMER"})

    assert response.status_code == 422
