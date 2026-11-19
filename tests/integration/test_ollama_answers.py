"""Grounded answers from the real local chat model (Qwen3 via Ollama). Free; skipped if
Ollama is not running. Retrieval uses fake embeddings: this checks the generation side."""

import pytest
from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.rag.answer import answer_question
from app.rag.embeddings import FakeEmbeddings
from app.rag.llm import build_chat_model

pytestmark = pytest.mark.llm


@pytest.fixture
async def chat_model() -> BaseChatModel:
    model = build_chat_model(Settings(_env_file=None, llm_provider="ollama"))  # type: ignore[call-arg]
    try:
        await model.ainvoke("Reply with OK.")
    except Exception as exc:  # connection refused, model not pulled, ...
        pytest.skip(f"Ollama not available: {exc}")
    return model


async def test_the_answer_states_the_active_rule_and_cites_it(
    session_with_returns_policies: AsyncSession, chat_model: BaseChatModel
) -> None:
    answer = await answer_question(
        session_with_returns_policies,
        FakeEmbeddings(),
        chat_model,
        "Within how many days of delivery must a return request be submitted?",
        tenant_id="nsi-amer",
    )

    assert answer.status == "answered"
    assert "30" in answer.text
    assert "45" not in answer.text  # the superseded v2.1 rule is never a source
    assert {(c.doc_key, c.version) for c in answer.citations} == {("NSI-POL-RET-003", "3.0")}


async def test_a_question_the_documents_do_not_answer_is_refused(
    session_with_returns_policies: AsyncSession, chat_model: BaseChatModel
) -> None:
    answer = await answer_question(
        session_with_returns_policies,
        FakeEmbeddings(),
        chat_model,
        "How many weeks of parental leave do employees get?",
        tenant_id="nsi-amer",
    )

    assert answer.status == "insufficient_evidence"
