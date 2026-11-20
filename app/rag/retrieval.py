"""Retrieval: dense (meaning), keyword (exact words) and hybrid (both, fused with RRF).

    question -+-> embed ---> dense top-k    (pgvector <=>, HNSW index)  -+
              +-> tsquery -> keyword top-k  (ts_rank_cd, GIN index)     -+-> RRF -> top N

Why both:
- Embeddings find paraphrases and other languages ("send back" ~ "return", a Spanish
  question ~ an English policy), but they blur exact identifiers: NX-4400 and NX-4400B
  look almost identical to an embedding model.
- Full-text search is exact on words and codes, but blind to synonyms and languages.
- Reciprocal Rank Fusion merges the two rankings by *position*. A cosine similarity and a
  ts_rank_cd score are on different scales and cannot be added; positions can.
- The keyword ranking counts half (``KEYWORD_WEIGHT``). Chosen by evaluation
  (docs/benchmarks): with equal weights, chunks that both methods rank only mediocrely
  outscored a chunk ranked first by one of them. PostgreSQL's ts_rank_cd has no IDF (unlike
  BM25): common words count as much as a rare product code, so the keyword list is noisy.

Both searches use the same eligibility filter, written in SQL, so a document that must
not be seen is never a candidate at all (filtering *after* a top-k could leave nothing):
tenant, status (or the version in force on a date), classification and document keys.
Under Row-Level Security the tenant filter is the first layer, not the only one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import EMBEDDING_DIMENSIONS
from app.rag.embeddings import EmbeddingProvider

Mode = Literal["dense", "keyword", "hybrid"]

DEFAULT_LIMIT = 5
CANDIDATES = 20  # taken from each method before fusion
RRF_K = 60  # the constant from the original RRF paper; dampens the weight of the top ranks
KEYWORD_WEIGHT = 0.5  # relative to the dense ranking (1.0); see the module docstring


@dataclass(frozen=True, slots=True)
class SearchFilters:
    # The versions in force on this date (point-in-time questions) instead of the active ones.
    as_of: date | None = None
    # Only these classification levels. Role-based limits arrive with authentication.
    classifications: tuple[str, ...] | None = None
    doc_keys: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk_id: uuid.UUID
    doc_key: str
    version: str
    title: str
    classification: str
    page_number: int
    section_path: tuple[str, ...]
    content: str
    # Higher is better. dense: cosine similarity; keyword: ts_rank_cd; hybrid: RRF score.
    score: float
    # Cosine similarity to the question, whenever the dense search returned this chunk.
    similarity: float | None = field(default=None)


def _eligible_documents(filters: SearchFilters) -> str:
    """CTE with one row per eligible document version. Only constant SQL fragments are
    joined here; every value is a bound parameter."""
    conditions = ["d.tenant_id = :tenant_id"]
    if filters.as_of is None:
        conditions.append("d.status = 'active'")
    else:
        conditions += ["d.status IN ('active', 'superseded')", "d.effective_date <= :as_of"]
    if filters.classifications is not None:
        conditions.append("d.classification = ANY(:classifications)")
    if filters.doc_keys is not None:
        conditions.append("d.doc_key = ANY(:doc_keys)")
    where = "\n          AND ".join(conditions)
    return f"""
    eligible AS (
        -- One version per document: the active one, or the latest in force on the as_of date.
        SELECT DISTINCT ON (d.doc_key) d.id, d.doc_key, d.version, d.title, d.classification
        FROM documents AS d
        WHERE {where}
        ORDER BY d.doc_key, d.effective_date DESC
    )"""  # noqa: S608 (constant fragments only)


_COLUMNS = """c.id, e.doc_key, e.version, e.title, e.classification, c.page_number,
           c.section_path, c.content"""


def _dense_sql(filters: SearchFilters) -> str:
    return f"""
    WITH {_eligible_documents(filters)}
    SELECT {_COLUMNS}, 1 - (c.embedding <=> :query_embedding) AS score
    FROM chunks AS c
    JOIN eligible AS e ON e.id = c.document_id
    WHERE c.tenant_id = :tenant_id
      AND c.embedding_model = :embedding_model
    ORDER BY c.embedding <=> :query_embedding
    LIMIT :limit
    """  # noqa: S608 (constant fragments only, see _eligible_documents)


def _keyword_sql(filters: SearchFilters) -> str:
    return f"""
    WITH {_eligible_documents(filters)},
    query AS (
        -- plainto_tsquery ANDs every word, and a question rarely contains them all:
        -- OR them instead, and let ts_rank_cd reward chunks that match more words.
        SELECT to_tsquery('english',
                          replace(plainto_tsquery('english', :query)::text, ' & ', ' | ')) AS q
    )
    SELECT {_COLUMNS}, ts_rank_cd(c.tsv, query.q) AS score
    FROM chunks AS c
    JOIN eligible AS e ON e.id = c.document_id
    CROSS JOIN query
    WHERE c.tenant_id = :tenant_id
      AND c.tsv @@ query.q
    ORDER BY score DESC, c.id
    LIMIT :limit
    """  # noqa: S608 (constant fragments only, see _eligible_documents)


def _params(tenant_id: str, filters: SearchFilters, limit: int) -> dict[str, Any]:
    params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
    if filters.as_of is not None:
        params["as_of"] = filters.as_of
    if filters.classifications is not None:
        params["classifications"] = list(filters.classifications)
    if filters.doc_keys is not None:
        params["doc_keys"] = list(filters.doc_keys)
    return params


def _bind_arrays(statement: Any, filters: SearchFilters) -> Any:
    for name in ("classifications", "doc_keys"):
        if getattr(filters, name) is not None:
            statement = statement.bindparams(bindparam(name, type_=ARRAY(Text)))
    return statement


def _hits(rows: Any, *, dense: bool) -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=row.id,
            doc_key=row.doc_key,
            version=row.version,
            title=row.title,
            classification=row.classification,
            page_number=row.page_number,
            section_path=tuple(row.section_path),
            content=row.content,
            score=float(row.score),
            similarity=float(row.score) if dense else None,
        )
        for row in rows
    ]


async def dense_search(
    session: AsyncSession,
    embeddings: EmbeddingProvider,
    query: str,
    *,
    tenant_id: str,
    filters: SearchFilters,
    limit: int,
) -> list[SearchHit]:
    query_embedding = await embeddings.embed_query(query)
    # Through the HNSW index, the WHERE filters only see the candidates one index pass
    # returns (hnsw.ef_search, 40 by default): a tenant with few chunks could get fewer
    # than `limit` results, or none. Iterative scans keep walking the graph until enough
    # rows pass the filters (pgvector >= 0.8). SET LOCAL ends with the transaction.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    statement = text(_dense_sql(filters)).bindparams(
        bindparam("query_embedding", type_=Vector(EMBEDDING_DIMENSIONS))
    )
    params = _params(tenant_id, filters, limit) | {
        "query_embedding": query_embedding,
        # Vectors from different models live in different spaces: never compare them.
        "embedding_model": embeddings.model_id,
    }
    rows = await session.execute(_bind_arrays(statement, filters), params)
    return _hits(rows, dense=True)


async def keyword_search(
    session: AsyncSession, query: str, *, tenant_id: str, filters: SearchFilters, limit: int
) -> list[SearchHit]:
    statement = _bind_arrays(text(_keyword_sql(filters)), filters)
    rows = await session.execute(statement, _params(tenant_id, filters, limit) | {"query": query})
    return _hits(rows, dense=False)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]],
    *,
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> list[SearchHit]:
    """Merge rankings: score(chunk) = sum over rankings of weight / (k + position), from 1.

    A chunk ranked high by both methods beats one ranked first by only one of them. The
    chunk keeps the dense similarity if the dense ranking had it.
    """
    weights = weights or [1.0] * len(rankings)
    scores: dict[uuid.UUID, float] = {}
    hits: dict[uuid.UUID, SearchHit] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for position, hit in enumerate(ranking, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + weight / (k + position)
            known = hits.get(hit.chunk_id)
            if known is None or (known.similarity is None and hit.similarity is not None):
                hits[hit.chunk_id] = hit
    ordered = sorted(scores, key=lambda chunk_id: scores[chunk_id], reverse=True)
    return [replace(hits[chunk_id], score=scores[chunk_id]) for chunk_id in ordered]


async def search_chunks(
    session: AsyncSession,
    embeddings: EmbeddingProvider,
    query: str,
    *,
    tenant_id: str,
    limit: int = DEFAULT_LIMIT,
    mode: Mode = "hybrid",
    filters: SearchFilters | None = None,
    candidates: int = CANDIDATES,
) -> list[SearchHit]:
    """The ``limit`` eligible chunks of ``tenant_id`` that best match ``query``."""
    filters = filters or SearchFilters()
    if mode == "dense":
        return await dense_search(
            session, embeddings, query, tenant_id=tenant_id, filters=filters, limit=limit
        )
    if mode == "keyword":
        return await keyword_search(
            session, query, tenant_id=tenant_id, filters=filters, limit=limit
        )
    dense = await dense_search(
        session, embeddings, query, tenant_id=tenant_id, filters=filters, limit=candidates
    )
    keyword = await keyword_search(
        session, query, tenant_id=tenant_id, filters=filters, limit=candidates
    )
    return reciprocal_rank_fusion([dense, keyword], weights=[1.0, KEYWORD_WEIGHT])[:limit]
