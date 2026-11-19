"""POST /v1/ask over HTTP: retrieval + generation + citation checks (fake models)."""

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ask import get_chat_model
from app.api.dependencies import get_embeddings, get_session
from app.main import create_app
from app.rag.embeddings import FakeEmbeddings
from app.rag.llm import FakeGroundedChatModel

pytestmark = pytest.mark.integration

AMER = {"X-Tenant-ID": "nsi-amer"}
QUESTION = {"question": "When must a return request be submitted?"}

ClientFactory = Callable[[BaseChatModel], httpx.AsyncClient]


@pytest.fixture
async def client_with(
    session_with_returns_policies: AsyncSession,
) -> AsyncIterator[ClientFactory]:
    clients: list[httpx.AsyncClient] = []

    def make(chat_model: BaseChatModel) -> httpx.AsyncClient:
        app = create_app()
        app.dependency_overrides[get_session] = lambda: session_with_returns_policies
        app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddings()
        app.dependency_overrides[get_chat_model] = lambda: chat_model
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


def scripted(text: str) -> BaseChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content=text)]))


async def test_an_answer_cites_the_page_it_comes_from(client_with: ClientFactory) -> None:
    response = await client_with(FakeGroundedChatModel()).post(
        "/v1/ask", json=QUESTION, headers=AMER
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "answered"
    assert body["answer"].endswith("[1]")
    [citation] = body["citations"]
    assert (citation["doc_key"], citation["version"]) == ("NSI-POL-RET-003", "3.0")


async def test_without_sources_the_model_is_not_called(client_with: ClientFactory) -> None:
    never = scripted("this would be an invented answer [1]")

    response = await client_with(never).post(
        "/v1/ask",
        json={**QUESTION, "filters": {"classifications": ["public"]}},  # nothing is public
        headers=AMER,
    )

    assert (response.json()["status"], response.json()["reason"]) == (
        "insufficient_evidence",
        "no_sources",
    )


async def test_an_invented_citation_is_not_shown(client_with: ClientFactory) -> None:
    response = await client_with(scripted("Returns are accepted for 90 days [42].")).post(
        "/v1/ask", json=QUESTION, headers=AMER
    )

    body = response.json()
    assert (body["status"], body["reason"]) == ("insufficient_evidence", "invalid_citations")
    assert "90 days" not in body["answer"]


async def test_a_request_body_cannot_choose_the_tenant(client_with: ClientFactory) -> None:
    response = await client_with(FakeGroundedChatModel()).post(
        "/v1/ask", json={**QUESTION, "tenant_id": "nsi-emea"}, headers=AMER
    )

    assert response.status_code == 422
