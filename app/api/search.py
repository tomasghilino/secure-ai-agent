"""Semantic search endpoint: the retrieval step of RAG, on its own.

It shows exactly which evidence a question finds before any answer is generated, which
is also how retrieval quality is evaluated separately from generation later.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import EmbeddingsDep, SessionDep, TenantDep
from app.rag.retrieval import DEFAULT_LIMIT, search_chunks

router = APIRouter(prefix="/v1", tags=["search"])


class SearchRequest(BaseModel):
    # extra="forbid": a body that tries to carry a tenant_id is rejected (422), not ignored.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=20)


class SearchResult(BaseModel):
    doc_key: str
    version: str
    title: str
    page_number: int
    section_path: list[str]
    content: str
    similarity: float


class SearchResponse(BaseModel):
    embedding_model: str
    results: list[SearchResult]


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    tenant_id: TenantDep,
    session: SessionDep,
    embeddings: EmbeddingsDep,
) -> SearchResponse:
    hits = await search_chunks(
        session, embeddings, body.query, tenant_id=tenant_id, limit=body.limit
    )
    return SearchResponse(
        embedding_model=embeddings.model_id,
        results=[
            SearchResult(
                doc_key=hit.doc_key,
                version=hit.version,
                title=hit.title,
                page_number=hit.page_number,
                section_path=list(hit.section_path),
                content=hit.content,
                similarity=round(hit.similarity, 4),
            )
            for hit in hits
        ],
    )
