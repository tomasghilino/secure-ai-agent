"""Question answering over the documents: retrieval + grounded generation (RAG)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import EmbeddingsDep, SessionDep, SettingsDep, TenantDep
from app.api.search import FiltersModel
from app.rag.answer import Reason, Status, answer_question

router = APIRouter(prefix="/v1", tags=["ask"])


def get_chat_model(request: Request) -> BaseChatModel:
    chat_model: BaseChatModel = request.app.state.chat_model
    return chat_model


ChatModelDep = Annotated[BaseChatModel, Depends(get_chat_model)]


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=2000)
    filters: FiltersModel = Field(default_factory=FiltersModel)


class CitationModel(BaseModel):
    number: int
    doc_key: str
    version: str
    title: str
    page_number: int
    section: str


class AskResponse(BaseModel):
    status: Status
    answer: str
    citations: list[CitationModel]
    reason: Reason | None = Field(description="Why there is no answer, when there is none")
    sources_given: int
    usage: dict[str, Any] | None


@router.post("/ask")
async def ask(
    body: AskRequest,
    tenant_id: TenantDep,
    settings: SettingsDep,
    session: SessionDep,
    embeddings: EmbeddingsDep,
    chat_model: ChatModelDep,
) -> AskResponse:
    answer = await answer_question(
        session,
        embeddings,
        chat_model,
        body.question,
        tenant_id=tenant_id,
        filters=body.filters.to_filters(),
        mode=settings.rag_retrieval_mode,
        top_k=settings.rag_top_k,
        max_context_tokens=settings.rag_max_context_tokens,
    )
    return AskResponse(
        status=answer.status,
        answer=answer.text,
        citations=[CitationModel(**asdict(citation)) for citation in answer.citations],
        reason=answer.reason,
        sources_given=answer.sources_given,
        usage=answer.usage,
    )
