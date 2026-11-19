"""Ingestion into a real PostgreSQL, with fake embeddings (free and deterministic)."""

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

import pytest
from alembic import command
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ChunkRecord, DocumentRecord
from app.ingestion.pipeline import DocumentMetadata, IngestionResult, ingest_pdf
from app.rag.embeddings import FakeEmbeddings
from tests.conftest import GeneratedPdfs
from tests.integration.conftest import RETURNS_V2_1, RETURNS_V3_0, alembic_config

pytestmark = pytest.mark.integration

RETURNS_POLICY = DocumentMetadata(
    tenant_id="nsi-amer",
    doc_key="NSI-POL-RET-003",
    version="3.0",
    title="Returns and Refunds Policy",
    status="active",
    effective_date=date(2026, 3, 1),
    classification="internal",
)


async def _count_chunks(session: AsyncSession) -> int | None:
    return await session.scalar(select(func.count()).select_from(ChunkRecord))


def test_migrations_match_the_models(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        command.check(alembic_config(connection))  # fails if a model change has no migration
    engine.dispose()


async def test_stores_the_document_and_one_row_per_chunk(
    session: AsyncSession, returns_policy_pdf: bytes
) -> None:
    result = await ingest_pdf(session, returns_policy_pdf, RETURNS_POLICY, FakeEmbeddings())

    assert not result.skipped
    assert result.chunk_count == 10
    document = await session.get_one(DocumentRecord, result.document_id)
    assert (document.doc_key, document.version, document.status) == (
        "NSI-POL-RET-003",
        "3.0",
        "active",
    )
    assert document.sha256 == hashlib.sha256(returns_policy_pdf).hexdigest()

    chunks = (
        await session.scalars(
            select(ChunkRecord)
            .where(ChunkRecord.document_id == document.id)
            .order_by(ChunkRecord.chunk_index)
        )
    ).all()
    assert [c.page_number for c in chunks] == [1, 1, 1, 1, 2, 2, 2, 2, 3, 3]
    assert chunks[3].section_path == ["Returns and Refunds Policy", "3. Return Eligibility"]
    assert chunks[3].content.startswith("Returns and Refunds Policy > 3. Return Eligibility\n\n")
    assert all(c.tenant_id == "nsi-amer" for c in chunks)  # copied from the document
    assert all(len(c.embedding) == 1024 for c in chunks)
    assert {c.embedding_model for c in chunks} == {"fake-feature-hashing-1024"}


async def test_the_same_file_twice_is_skipped(
    session: AsyncSession, returns_policy_pdf: bytes
) -> None:
    first = await ingest_pdf(session, returns_policy_pdf, RETURNS_POLICY, FakeEmbeddings())
    second = await ingest_pdf(session, returns_policy_pdf, RETURNS_POLICY, FakeEmbeddings())

    assert second.skipped
    assert second.document_id == first.document_id
    assert await _count_chunks(session) == 10  # not 20


async def test_two_workers_with_the_same_file_store_it_once(
    sessions: async_sessionmaker[AsyncSession], returns_policy_pdf: bytes
) -> None:
    async def worker() -> IngestionResult:
        async with sessions() as session:
            return await ingest_pdf(session, returns_policy_pdf, RETURNS_POLICY, FakeEmbeddings())

    # Both pass the cheap check before either has inserted (extraction takes a while).
    results = await asyncio.gather(worker(), worker())

    assert sorted(r.skipped for r in results) == [False, True]
    loser = next(r for r in results if r.skipped)
    assert loser.document_id is None  # stopped by ON CONFLICT, not by the cheap check
    async with sessions() as session:
        assert await _count_chunks(session) == 10


class CountingEmbeddings(FakeEmbeddings):
    """Fake embeddings that count how many texts were actually sent to the model."""

    def __init__(self) -> None:
        super().__init__()
        self.embedded = 0

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return await super().embed_documents(texts)


async def _statuses(session: AsyncSession, doc_key: str) -> dict[str, str]:
    async with session.begin():
        rows = await session.execute(
            select(DocumentRecord.version, DocumentRecord.status).where(
                DocumentRecord.doc_key == doc_key
            )
        )
        return {version: status for version, status in rows}


async def test_a_new_active_version_supersedes_the_previous_one(
    session: AsyncSession, generated_pdfs: GeneratedPdfs
) -> None:
    old_metadata, old_pdf = generated_pdfs[RETURNS_V2_1]
    new_metadata, new_pdf = generated_pdfs[RETURNS_V3_0]
    # v2.1 arrives first, still believed active (as it was before v3.0 existed).
    await ingest_pdf(session, old_pdf, replace(old_metadata, status="active"), FakeEmbeddings())

    result = await ingest_pdf(session, new_pdf, new_metadata, FakeEmbeddings())

    assert result.status == "active"
    assert await _statuses(session, "NSI-POL-RET-003") == {"2.1": "superseded", "3.0": "active"}


async def test_an_older_version_arriving_late_is_stored_as_superseded(
    session: AsyncSession, generated_pdfs: GeneratedPdfs
) -> None:
    old_metadata, old_pdf = generated_pdfs[RETURNS_V2_1]
    new_metadata, new_pdf = generated_pdfs[RETURNS_V3_0]
    await ingest_pdf(session, new_pdf, new_metadata, FakeEmbeddings())

    result = await ingest_pdf(
        session, old_pdf, replace(old_metadata, status="active"), FakeEmbeddings()
    )

    assert result.status == "superseded"  # v3.0 has a later effective date: it stays active
    assert await _statuses(session, "NSI-POL-RET-003") == {"2.1": "superseded", "3.0": "active"}


async def test_a_duplicate_upload_does_not_supersede_anything(
    sessions: async_sessionmaker[AsyncSession], returns_policy_pdf: bytes
) -> None:
    async def worker() -> IngestionResult:
        async with sessions() as session:
            return await ingest_pdf(session, returns_policy_pdf, RETURNS_POLICY, FakeEmbeddings())

    await asyncio.gather(worker(), worker())  # the loser's transaction must roll back

    async with sessions() as session:
        assert await _statuses(session, "NSI-POL-RET-003") == {"3.0": "active"}


async def test_unchanged_text_is_not_embedded_again(
    session: AsyncSession, returns_policy_pdf: bytes
) -> None:
    first = await ingest_pdf(session, returns_policy_pdf, RETURNS_POLICY, FakeEmbeddings())
    async with session.begin():
        await session.execute(delete(DocumentRecord))  # e.g. re-indexing from scratch
    counting = CountingEmbeddings()

    again = await ingest_pdf(session, returns_policy_pdf, RETURNS_POLICY, counting)

    assert again.cache_hits == again.chunk_count == first.chunk_count
    assert counting.embedded == 0  # every vector came from the cache: nothing to pay
