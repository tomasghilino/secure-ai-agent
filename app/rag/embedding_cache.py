"""Embedding cache: never pay twice for the same text with the same model.

    texts -> sha256 of each -> look up (tenant, model, dimensions, hash)
          -> embed only the misses -> store them -> vectors in the original order

A new version of a policy usually changes a few sections: its unchanged chunks are cache
hits, so re-ingesting it embeds (and, on Bedrock, bills) only the changed text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import EmbeddingCacheRecord
from app.rag.embeddings import EmbeddingProvider, Vector


@dataclass(frozen=True, slots=True)
class CachedEmbeddings:
    vectors: list[Vector]
    hits: int  # texts whose vector came from the cache


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def embed_documents_cached(
    session: AsyncSession, embeddings: EmbeddingProvider, tenant_id: str, texts: Sequence[str]
) -> CachedEmbeddings:
    hashes = [content_hash(text) for text in texts]
    same_model = (
        EmbeddingCacheRecord.tenant_id == tenant_id,
        EmbeddingCacheRecord.model_id == embeddings.model_id,
        EmbeddingCacheRecord.dimensions == embeddings.dimensions,
    )
    async with session.begin():
        rows = await session.execute(
            select(EmbeddingCacheRecord.content_sha256, EmbeddingCacheRecord.embedding).where(
                *same_model, EmbeddingCacheRecord.content_sha256.in_(set(hashes))
            )
        )
        found: dict[str, Vector] = {sha: [float(x) for x in vector] for sha, vector in rows}
    hits = sum(1 for sha in hashes if sha in found)

    # A dict also removes duplicate texts: each distinct text is embedded once.
    missing = {sha: text for sha, text in zip(hashes, texts, strict=True) if sha not in found}
    if missing:
        vectors = await embeddings.embed_documents(list(missing.values()))
        computed = dict(zip(missing, vectors, strict=True))
        async with session.begin():
            await session.execute(
                insert(EmbeddingCacheRecord).on_conflict_do_nothing(),
                [
                    {
                        "tenant_id": tenant_id,
                        "model_id": embeddings.model_id,
                        "dimensions": embeddings.dimensions,
                        "content_sha256": sha,
                        "embedding": vector,
                    }
                    for sha, vector in computed.items()
                ],
            )
        found.update(computed)
    return CachedEmbeddings([found[sha] for sha in hashes], hits)
