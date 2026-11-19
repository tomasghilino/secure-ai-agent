"""Asynchronous ingestion: the API registers a job, the worker processes it.

    API      validate -> already indexed? -> store the PDF -> insert job (queued)
             -> send {job_id, tenant_id}
    worker   receive -> claim the job (running) -> read the PDF -> ingest_pdf
             -> succeeded | failed -> delete the message
    sweeper  re-send jobs whose message was lost; fail jobs nobody finished

The API never extracts or embeds: an upload answers in milliseconds, and a slow or failing
model cannot block it. Version conflicts are rejected *before* anything is queued, so no
embeddings are paid for a file that would be refused at the end.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DocumentRecord, IngestionJobRecord
from app.ingestion.extraction import PdfValidationError
from app.ingestion.pipeline import DocumentMetadata, IngestionError, ingest_pdf
from app.ingestion.queue import JobQueue, JobRef
from app.ingestion.storage import ObjectNotFoundError, ObjectStorage, upload_key
from app.rag.embeddings import EmbeddingProvider

IN_FLIGHT = ("queued", "running")
# Retrying cannot fix these: the job fails and its message is deleted.
PERMANENT_ERRORS = (IngestionError, PdfValidationError, ObjectNotFoundError)


class VersionConflictError(Exception):
    """This doc_key + version is already registered (or queued) with a different file."""


@dataclass(frozen=True, slots=True)
class Submission:
    status: Literal["queued", "already_indexed"]
    job_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None


async def submit_document(
    session: AsyncSession,
    storage: ObjectStorage,
    queue: JobQueue,
    data: bytes,
    metadata: DocumentMetadata,
) -> Submission:
    sha256 = hashlib.sha256(data).hexdigest()
    same_file = and_(
        DocumentRecord.tenant_id == metadata.tenant_id, DocumentRecord.sha256 == sha256
    )
    same_version = and_(
        DocumentRecord.tenant_id == metadata.tenant_id,
        DocumentRecord.doc_key == metadata.doc_key,
        DocumentRecord.version == metadata.version,
    )
    async with session.begin():
        documents = (
            await session.scalars(select(DocumentRecord).where(or_(same_file, same_version)))
        ).all()
        jobs = (
            await session.scalars(
                select(IngestionJobRecord).where(
                    IngestionJobRecord.tenant_id == metadata.tenant_id,
                    IngestionJobRecord.status.in_(IN_FLIGHT),
                    or_(
                        IngestionJobRecord.sha256 == sha256,
                        and_(
                            IngestionJobRecord.doc_key == metadata.doc_key,
                            IngestionJobRecord.version == metadata.version,
                        ),
                    ),
                )
            )
        ).all()

    if indexed := next((d for d in documents if d.sha256 == sha256), None):
        return Submission("already_indexed", document_id=indexed.id)
    if pending := next((j for j in jobs if j.sha256 == sha256), None):
        # Same file uploaded again while its job waits: send the job again instead of
        # creating a second one. A duplicate message is harmless (the worker is
        # idempotent), and it rescues a job whose first message was lost.
        await queue.send(JobRef(pending.id, metadata.tenant_id))
        return Submission("queued", job_id=pending.id)
    if documents or jobs:
        raise VersionConflictError(
            f"{metadata.doc_key} v{metadata.version} is already registered with a different file"
        )

    key = upload_key(metadata.tenant_id, sha256)
    await storage.put(key, data)
    async with session.begin():
        job = IngestionJobRecord(
            tenant_id=metadata.tenant_id,
            status="queued",
            attempts=0,
            sha256=sha256,
            storage_key=key,
            doc_key=metadata.doc_key,
            version=metadata.version,
            title=metadata.title,
            document_status=metadata.status,
            effective_date=metadata.effective_date,
            classification=metadata.classification,
        )
        session.add(job)
    try:
        await queue.send(JobRef(job.id, metadata.tenant_id))
    except Exception:
        # Committed but never sent: no worker would ever see it. (A crash right here still
        # leaves it waiting; uploading the same file again re-sends it.)
        async with session.begin():
            job.status, job.error = "failed", "could not enqueue the job"
        raise
    return Submission("queued", job_id=job.id)


def _metadata(job: IngestionJobRecord) -> DocumentMetadata:
    return DocumentMetadata(
        tenant_id=job.tenant_id,
        doc_key=job.doc_key,
        version=job.version,
        title=job.title,
        status=job.document_status,
        effective_date=job.effective_date,
        classification=job.classification,
    )


async def process_job(
    session: AsyncSession,
    storage: ObjectStorage,
    embeddings: EmbeddingProvider,
    job_id: uuid.UUID,
) -> str | None:
    """Run one job and return its final status, or None if there was nothing to do.

    Safe to run more than once for the same job, which happens because the queue delivers
    at least once: a finished job is skipped, and ``ingest_pdf`` skips a file that is
    already stored. Permanent failures mark the job failed. Any other error (database or
    model down) propagates, so the message is not deleted and is retried later.
    """
    async with session.begin():
        # FOR UPDATE: two deliveries of the same message claim the job one after the other.
        job = await session.get(IngestionJobRecord, job_id, with_for_update=True)
        if job is None or job.status in ("succeeded", "failed"):
            return None
        job.status = "running"
        job.attempts += 1

    try:
        data = await storage.get(job.storage_key)
        result = await ingest_pdf(session, data, _metadata(job), embeddings)
    except PERMANENT_ERRORS as exc:
        error = str(exc)
    except IntegrityError:
        # Another file took this doc_key + version after the upload was checked.
        error = f"{job.doc_key} v{job.version} is already registered with a different file"
    else:
        async with session.begin():
            # A skip by ON CONFLICT does not return the id: look up the stored file.
            document_id = result.document_id or await session.scalar(
                select(DocumentRecord.id).where(
                    DocumentRecord.tenant_id == job.tenant_id, DocumentRecord.sha256 == job.sha256
                )
            )
            job.status, job.error, job.document_id = "succeeded", None, document_id
        return job.status

    async with session.begin():
        job.status, job.error = "failed", error
    return job.status


async def fail_job(session: AsyncSession, job_id: uuid.UUID, error: str) -> None:
    """Mark a job failed, e.g. after its last delivery hit a transient error."""
    async with session.begin():
        await session.execute(
            update(IngestionJobRecord)
            .where(IngestionJobRecord.id == job_id, IngestionJobRecord.status.in_(IN_FLIGHT))
            .values(status="failed", error=error)
        )


async def sweep_jobs(
    session: AsyncSession,
    queue: JobQueue,
    *,
    requeue_after: timedelta = timedelta(minutes=5),
    abandon_after: timedelta = timedelta(minutes=15),
) -> tuple[int, int]:
    """Repair jobs that the normal flow left behind. Returns (re-sent, abandoned).

    - ``queued`` for too long: its message was probably lost (the API crashed between the
      commit and the send: a "dual write"). Send it again; a duplicate is harmless.
    - ``running`` for too long: every delivery failed or the worker died, and the message
      is in the dead-letter queue by now. Mark it failed so it does not look alive.

    Uses a session *without* tenant: looking across tenants is its job. Each UPDATE
    touches ``updated_at``, so two workers sweeping at once never pick the same job.
    """
    async with session.begin():
        requeued = (
            await session.execute(
                update(IngestionJobRecord)
                .where(
                    IngestionJobRecord.status == "queued",
                    IngestionJobRecord.updated_at < func.now() - requeue_after,
                )
                .values(updated_at=func.now())
                .returning(IngestionJobRecord.id, IngestionJobRecord.tenant_id)
            )
        ).all()
        abandoned = (
            await session.execute(
                update(IngestionJobRecord)
                .where(
                    IngestionJobRecord.status == "running",
                    IngestionJobRecord.updated_at < func.now() - abandon_after,
                )
                .values(status="failed", error="abandoned: no worker finished it in time")
                .returning(IngestionJobRecord.id)
            )
        ).all()
    for job_id, tenant_id in requeued:
        await queue.send(JobRef(job_id, tenant_id))
    return len(requeued), len(abandoned)
