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

Two more rules, both inside that transaction:

- **One active version per document.** A new ``active`` version supersedes the previous
  active one. If a newer version (later effective date) is already active, the incoming
  one is stored as ``superseded`` instead. A partial unique index enforces the rule.
- **Embedding cache.** Chunks whose exact text was already embedded with this model are
  not embedded again (``app/rag/embedding_cache.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import EMBEDDING_DIMENSIONS, ChunkRecord, DocumentRecord
from app.ingestion.chunking import DEFAULT_MAX_CHARS, Chunk, chunk_pages
from app.ingestion.extraction import extract_pdf
from app.ingestion.normalization import normalize_pages
from app.rag.embedding_cache import embed_documents_cached
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
    cache_hits: int = 0  # chunks whose embedding came from the cache
    status: str | None = None  # stored status (an "active" upload may become "superseded")


class _AlreadyStored(Exception):
    """Raised inside the transaction to roll it back: another worker stored the file."""


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
    embedded = await embed_documents_cached(
        session, embeddings, metadata.tenant_id, [chunk.text for chunk in chunks]
    )

    try:
        # One transaction: the document, its chunks and the status change, or nothing.
        async with session.begin():
            status = await _resolve_status(session, metadata)
            # Layer 2: if another worker inserted the same file meanwhile, insert nothing.
            document_id = await session.scalar(
                insert(DocumentRecord)
                .values(sha256=sha256, **(asdict(metadata) | {"status": status}))
                .on_conflict_do_nothing(index_elements=["tenant_id", "sha256"])
                .returning(DocumentRecord.id)
            )
            if document_id is None:
                raise _AlreadyStored  # also undoes the supersede done by _resolve_status
            await _insert_chunks(
                session, document_id, metadata, chunks, embedded.vectors, embeddings
            )
    except _AlreadyStored:
        return IngestionResult(skipped=True)
    return IngestionResult(
        skipped=False,
        document_id=document_id,
        chunk_count=len(chunks),
        cache_hits=embedded.hits,
        status=status,
    )


async def _resolve_status(session: AsyncSession, metadata: DocumentMetadata) -> str:
    """The status to store; supersedes the current active version when this one replaces it."""
    if metadata.status != "active":
        return metadata.status
    same_document = (
        DocumentRecord.tenant_id == metadata.tenant_id,
        DocumentRecord.doc_key == metadata.doc_key,
        DocumentRecord.status == "active",
    )
    newer = await session.scalar(
        select(DocumentRecord.id).where(
            *same_document, DocumentRecord.effective_date > metadata.effective_date
        )
    )
    if newer is not None:
        return "superseded"  # an older version arrived after a newer one
    await session.execute(update(DocumentRecord).where(*same_document).values(status="superseded"))
    return "active"


async def _insert_chunks(
    session: AsyncSession,
    document_id: uuid.UUID,
    metadata: DocumentMetadata,
    chunks: list[Chunk],
    vectors: list[list[float]],
    embeddings: EmbeddingProvider,
) -> None:
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
