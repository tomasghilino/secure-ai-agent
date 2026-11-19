"""Search endpoint: the retrieval step of RAG, on its own.

It shows exactly which evidence a question finds before any answer is generated, which
is also how retrieval quality is evaluated separately from generation.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import EmbeddingsDep, SessionDep, TenantDep
from app.rag.retrieval import DEFAULT_LIMIT, Mode, SearchFilters, SearchHit, search_chunks

router = APIRouter(prefix="/v1", tags=["search"])

Classification = Literal["public", "internal", "confidential", "restricted"]


class FiltersModel(BaseModel):
    """Optional narrowing. The tenant is never here: it comes from TenantDep."""

    model_config = ConfigDict(extra="forbid")

    as_of: date | None = Field(
        default=None, description="Use the versions in force on this date (point in time)"
    )
    classifications: list[Classification] | None = Field(default=None, min_length=1)
    doc_keys: list[str] | None = Field(default=None, min_length=1, max_length=20)

    def to_filters(self) -> SearchFilters:
        return SearchFilters(
            as_of=self.as_of,
            classifications=tuple(self.classifications) if self.classifications else None,
            doc_keys=tuple(self.doc_keys) if self.doc_keys else None,
        )


class SearchRequest(BaseModel):
    # extra="forbid": a body that tries to carry a tenant_id is rejected (422), not ignored.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=20)
    mode: Mode = "hybrid"
    filters: FiltersModel = Field(default_factory=FiltersModel)


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    doc_key: str
    version: str
    title: str
    classification: str
    page_number: int
    section_path: list[str]
    content: str
    score: float
    similarity: float | None

    @classmethod
    def from_hit(cls, hit: SearchHit) -> SearchResult:
        return cls(
            chunk_id=hit.chunk_id,
            doc_key=hit.doc_key,
            version=hit.version,
            title=hit.title,
            classification=hit.classification,
            page_number=hit.page_number,
            section_path=list(hit.section_path),
            content=hit.content,
            score=round(hit.score, 4),
            similarity=None if hit.similarity is None else round(hit.similarity, 4),
        )


class SearchResponse(BaseModel):
    mode: Mode
    embedding_model: str
    results: list[SearchResult]


@router.post("/search")
async def search(
    body: SearchRequest,
    tenant_id: TenantDep,
    session: SessionDep,
    embeddings: EmbeddingsDep,
) -> SearchResponse:
    hits = await search_chunks(
        session,
        embeddings,
        body.query,
        tenant_id=tenant_id,
        limit=body.limit,
        mode=body.mode,
        filters=body.filters.to_filters(),
    )
    return SearchResponse(
        mode=body.mode,
        embedding_model=embeddings.model_id,
        results=[SearchResult.from_hit(hit) for hit in hits],
    )
