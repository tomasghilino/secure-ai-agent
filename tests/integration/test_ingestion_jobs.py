"""Upload -> job -> worker, against a real PostgreSQL (in-memory queue, fake embeddings)."""

import pymupdf
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ChunkRecord, DocumentRecord, IngestionJobRecord
from app.ingestion.jobs import VersionConflictError, process_job, submit_document
from app.ingestion.storage import LocalFileStorage
from app.rag.embeddings import FakeEmbeddings
from tests.conftest import GeneratedPdfs
from tests.integration.conftest import RETURNS_V3_0, SUPPORT_SLA, InMemoryJobQueue

pytestmark = pytest.mark.integration


class ModelDown(FakeEmbeddings):
    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise ConnectionError("embedding model unreachable")


async def _count(session: AsyncSession, table: type) -> int | None:
    return await session.scalar(select(func.count()).select_from(table))


async def test_an_upload_stores_the_pdf_and_queues_a_job(
    session: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[RETURNS_V3_0]

    submission = await submit_document(session, storage, queue, pdf, metadata)

    assert submission.status == "queued"
    assert queue.sent_ids == [submission.job_id]
    job = await session.get_one(IngestionJobRecord, submission.job_id)
    assert (job.status, job.attempts, job.doc_key, job.version) == (
        "queued",
        0,
        "NSI-POL-RET-003",
        "3.0",
    )
    assert await storage.get(job.storage_key) == pdf
    assert await _count(session, DocumentRecord) == 0  # nothing is extracted or embedded yet


async def test_the_same_file_twice_reuses_the_waiting_job(
    session: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[RETURNS_V3_0]

    first = await submit_document(session, storage, queue, pdf, metadata)
    second = await submit_document(session, storage, queue, pdf, metadata)

    assert second.job_id == first.job_id
    assert queue.sent_ids == [first.job_id, first.job_id]  # sent again, not a second job
    assert await _count(session, IngestionJobRecord) == 1


async def test_an_indexed_file_is_not_queued_again(
    session_with_returns_policies: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[RETURNS_V3_0]

    submission = await submit_document(session_with_returns_policies, storage, queue, pdf, metadata)

    assert submission.status == "already_indexed"
    assert submission.document_id is not None
    assert queue.sent == []


async def test_another_file_for_an_indexed_version_is_rejected_before_queuing(
    session_with_returns_policies: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, _ = generated_pdfs[RETURNS_V3_0]
    _, other_pdf = generated_pdfs[SUPPORT_SLA]  # a different file claiming to be RET-003 v3.0

    with pytest.raises(VersionConflictError):
        await submit_document(session_with_returns_policies, storage, queue, other_pdf, metadata)

    assert queue.sent == []  # no embeddings will be paid for a file that cannot be stored


async def test_another_file_for_a_queued_version_is_rejected(
    session: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[RETURNS_V3_0]
    _, other_pdf = generated_pdfs[SUPPORT_SLA]
    await submit_document(session, storage, queue, pdf, metadata)

    with pytest.raises(VersionConflictError):
        await submit_document(session, storage, queue, other_pdf, metadata)


async def test_the_worker_ingests_a_queued_job(
    session: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[RETURNS_V3_0]
    submission = await submit_document(session, storage, queue, pdf, metadata)
    assert submission.job_id is not None

    status = await process_job(session, storage, FakeEmbeddings(), submission.job_id)

    assert status == "succeeded"
    job = await session.get_one(IngestionJobRecord, submission.job_id)
    assert (job.attempts, job.error) == (1, None)
    document = await session.get_one(DocumentRecord, job.document_id)
    assert (document.doc_key, document.version, document.status) == (
        "NSI-POL-RET-003",
        "3.0",
        "active",
    )
    assert await _count(session, ChunkRecord) == 10


async def test_a_job_delivered_twice_is_processed_once(
    session: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[RETURNS_V3_0]
    submission = await submit_document(session, storage, queue, pdf, metadata)
    assert submission.job_id is not None

    await process_job(session, storage, FakeEmbeddings(), submission.job_id)
    second = await process_job(session, storage, FakeEmbeddings(), submission.job_id)

    assert second is None  # already succeeded: nothing to do
    assert (await session.get_one(IngestionJobRecord, submission.job_id)).attempts == 1
    assert await _count(session, ChunkRecord) == 10


async def test_a_pdf_without_text_fails_the_job_for_good(
    session: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, _ = generated_pdfs[RETURNS_V3_0]
    blank = pymupdf.open()
    blank.new_page()  # like a scanned page: no text layer
    submission = await submit_document(session, storage, queue, blank.tobytes(), metadata)
    assert submission.job_id is not None

    status = await process_job(session, storage, FakeEmbeddings(), submission.job_id)

    assert status == "failed"
    job = await session.get_one(IngestionJobRecord, submission.job_id)
    assert job.error is not None
    assert "no text could be extracted" in job.error


async def test_a_transient_error_leaves_the_job_for_a_retry(
    session: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[RETURNS_V3_0]
    submission = await submit_document(session, storage, queue, pdf, metadata)
    assert submission.job_id is not None

    with pytest.raises(ConnectionError):  # propagates: the message must not be deleted
        await process_job(session, storage, ModelDown(), submission.job_id)
    retried = await process_job(session, storage, FakeEmbeddings(), submission.job_id)

    assert retried == "succeeded"
    assert (await session.get_one(IngestionJobRecord, submission.job_id)).attempts == 2
