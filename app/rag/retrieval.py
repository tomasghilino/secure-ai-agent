"""Semantic search over the stored chunks (dense retrieval).

The query is embedded with the same model as the chunks, then PostgreSQL orders the
chunks by cosine distance to it (pgvector's ``<=>``). The SQL is written out in full
instead of hidden behind a vector-store library: tenant and status filters are part of
the query itself, so a superseded version or another tenant's document can never be
returned, and the same SQL grows into hybrid search later.

The HNSW index (``ix_chunks_embedding_hnsw``) makes the search approximate and fast on
large tables. On a small table PostgreSQL may still prefer an exact scan, which is also
correct: the planner chooses by cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import EMBEDDING_DIMENSIONS
from app.rag.embeddings import EmbeddingProvider

DEFAULT_LIMIT = 5

_SEARCH_SQL = text(
    """
    SELECT d.doc_key, d.version, d.title, c.page_number, c.section_path, c.content,
           c.embedding <=> :query_embedding AS distance
    FROM chunks AS c
    JOIN documents AS d ON d.id = c.document_id
    WHERE c.tenant_id = :tenant_id
      AND d.status = 'active'
      AND c.embedding_model = :embedding_model
    ORDER BY c.embedding <=> :query_embedding
    LIMIT :limit
    """
).bindparams(bindparam("query_embedding", type_=Vector(EMBEDDING_DIMENSIONS)))


@dataclass(frozen=True, slots=True)
class SearchHit:
    doc_key: str
    version: str
    title: str
    page_number: int
    section_path: tuple[str, ...]
    content: str
    distance: float  # cosine distance: 0 = same direction, 1 = unrelated

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance


async def search_chunks(
    session: AsyncSession,
    embeddings: EmbeddingProvider,
    query: str,
    *,
    tenant_id: str,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchHit]:
    """The ``limit`` active chunks of ``tenant_id`` closest in meaning to ``query``."""
    query_embedding = await embeddings.embed_query(query)
    # Through the HNSW index, the WHERE filters only see the candidates one index pass
    # returns (hnsw.ef_search, 40 by default): a tenant with few chunks could get fewer
    # than `limit` results, or none. Iterative scans keep walking the graph until enough
    # rows pass the filters (pgvector >= 0.8). SET LOCAL ends with the transaction.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    rows = await session.execute(
        _SEARCH_SQL,
        {
            "query_embedding": query_embedding,
            "tenant_id": tenant_id,
            # Vectors from different models live in different spaces: never compare them.
            "embedding_model": embeddings.model_id,
            "limit": limit,
        },
    )
    return [
        SearchHit(
            doc_key=row.doc_key,
            version=row.version,
            title=row.title,
            page_number=row.page_number,
            section_path=tuple(row.section_path),
            content=row.content,
            distance=row.distance,
        )
        for row in rows
    ]
