"""Ingestion into a real PostgreSQL, with fake embeddings (free and deterministic)."""

import asyncio
import hashlib
from datetime import date

import pytest
from alembic import command
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ChunkRecord, DocumentRecord
from app.ingestion.pipeline import DocumentMetadata, IngestionResult, ingest_pdf
from app.rag.embeddings import FakeEmbeddings
from tests.integration.conftest import alembic_config

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
