"""Ingestion pipeline: one PDF in, one document row and its embedded chunks out.

    sha256 -> already ingested? -> extract -> normalize -> chunk -> embed -> save

Idempotent in two layers, because a queue delivers messages *at least once* and users
upload the same file twice:

1. **Cheap check first.** If the tenant already has a file with the same sha256, stop
   before the expensive work (extraction, embedding calls).
2. **The database has the final word.** Two workers can pass the check at the same
   moment, so the insert uses ``ON CONFLICT (tenant_id, sha256) DO NOTHING``: the second
   one inserts nothing and reports "skipped" (the same SQL Prisma's
   ``createMany({ skipDuplicates: true })`` uses).

The document row and its chunks are written in one transaction: a crash halfway leaves
nothing behind, never a document with half of its chunks. Extraction and embedding run
*before* that transaction opens, so no transaction waits seconds for a model.

A different file registered under an existing ``doc_key`` + ``version`` is not a duplicate
but a conflict: that insert fails with ``IntegrityError`` instead of being skipped.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import EMBEDDING_DIMENSIONS, ChunkRecord, DocumentRecord
from app.ingestion.chunking import DEFAULT_MAX_CHARS, chunk_pages
from app.ingestion.extraction import extract_pdf
from app.ingestion.normalization import normalize_pages
from app.rag.embeddings import EmbeddingProvider


class IngestionError(RuntimeError):
    """The document cannot be ingested (for example, it has no extractable text)."""


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Registry facts about a document version. The PDF content cannot be trusted for these
    (an outdated PDF does not say it is outdated), so they come from the registry."""

    tenant_id: str
    doc_key: str
    version: str
    title: str
    status: str
    effective_date: date
    classification: str


@dataclass(frozen=True, slots=True)
class IngestionResult:
    skipped: bool  # True when this tenant already had the same file
    document_id: uuid.UUID | None = None
    chunk_count: int = 0


async def ingest_pdf(
    session: AsyncSession,
    data: bytes,
    metadata: DocumentMetadata,
    embeddings: EmbeddingProvider,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> IngestionResult:
    if embeddings.dimensions != EMBEDDING_DIMENSIONS:
        raise IngestionError(
            f"{embeddings.model_id} produces {embeddings.dimensions}-d vectors, "
            f"but the chunks table stores {EMBEDDING_DIMENSIONS}-d vectors"
        )
    sha256 = hashlib.sha256(data).hexdigest()

    # Layer 1: cheap check before the expensive work.
    async with session.begin():
        existing = await session.scalar(
            select(DocumentRecord.id).where(
                DocumentRecord.tenant_id == metadata.tenant_id, DocumentRecord.sha256 == sha256
            )
        )
    if existing is not None:
        return IngestionResult(skipped=True, document_id=existing)

    extracted = await asyncio.to_thread(extract_pdf, data)  # CPU-bound: keep the loop free
    chunks = chunk_pages(normalize_pages(extracted.pages).pages, max_chars=max_chars)
    if not chunks:
        raise IngestionError("no text could be extracted (scanned PDF? OCR is not supported)")
    vectors = await embeddings.embed_documents([chunk.text for chunk in chunks])

    async with session.begin():  # one transaction: the document and all its chunks, or nothing
        # Layer 2: if another worker inserted the same file meanwhile, insert nothing.
        document_id = await session.scalar(
            insert(DocumentRecord)
            .values(sha256=sha256, **asdict(metadata))
            .on_conflict_do_nothing(index_elements=["tenant_id", "sha256"])
            .returning(DocumentRecord.id)
        )
        if document_id is None:
            return IngestionResult(skipped=True)
        await session.execute(
            insert(ChunkRecord),
            [
                {
                    "document_id": document_id,
                    "tenant_id": metadata.tenant_id,
                    "chunk_index": chunk.index,
                    "page_number": chunk.page_number,
                    "section_path": list(chunk.section_path),
                    "content": chunk.text,
                    "embedding": vector,
                    "embedding_model": embeddings.model_id,
                }
                for chunk, vector in zip(chunks, vectors, strict=True)
            ],
        )
    return IngestionResult(skipped=False, document_id=document_id, chunk_count=len(chunks))
