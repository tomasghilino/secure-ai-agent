"""Document upload and ingestion job status.

The upload only validates, stores the PDF and queues a job (202 Accepted); the worker
extracts, chunks and embeds it later. Clients poll ``GET /v1/jobs/{job_id}``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.dependencies import JobQueueDep, SessionDep, SettingsDep, StorageDep, TenantDep
from app.database.models import IngestionJobRecord
from app.ingestion.extraction import PDF_MAGIC
from app.ingestion.jobs import VersionConflictError, submit_document
from app.ingestion.pipeline import DocumentMetadata

router = APIRouter(prefix="/v1", tags=["documents"])


class UploadForm(BaseModel):
    """The PDF and its registry metadata. The tenant is not here: it comes from TenantDep."""

    # The file is a field of the model too: with extra="forbid", a file declared as a
    # separate parameter would count as an unexpected field.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    file: UploadFile = Field(description="The PDF")
    doc_key: str = Field(pattern=r"^[A-Z0-9][A-Z0-9-]{1,63}$", examples=["NSI-POL-RET-003"])
    version: str = Field(pattern=r"^[0-9]{1,4}(\.[0-9]{1,4}){0,2}$", examples=["3.0"])
    title: str = Field(min_length=1, max_length=300)
    effective_date: date
    classification: Literal["public", "internal", "confidential", "restricted"]
    status: Literal["draft", "active", "superseded"] = "active"


class UploadResponse(BaseModel):
    status: Literal["queued", "already_indexed"]
    job_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None


class JobResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    doc_key: str
    version: str
    attempts: int
    error: str | None
    document_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@router.post(
    "/documents",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {"model": UploadResponse, "description": "The same file is already indexed"},
        409: {"description": "This version is already registered with a different file"},
        413: {"description": "The file is too large"},
        415: {"description": "The file is not a PDF"},
    },
)
async def upload_document(
    form: Annotated[UploadForm, Form()],
    tenant_id: TenantDep,
    settings: SettingsDep,
    session: SessionDep,
    storage: StorageDep,
    queue: JobQueueDep,
    response: Response,
) -> UploadResponse:
    # Read at most one byte over the limit, so memory stays bounded. The request body is
    # already spooled to disk by now: a hard limit belongs in the load balancer too.
    data = await form.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "the file is too large")
    # The client's content type is a claim; the first bytes of the file are evidence.
    if form.file.content_type != "application/pdf" or not data.startswith(PDF_MAGIC):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "the file is not a PDF")

    metadata = DocumentMetadata(tenant_id=tenant_id, **form.model_dump(exclude={"file"}))
    try:
        submission = await submit_document(session, storage, queue, data, metadata)
    except VersionConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if submission.status == "already_indexed":
        response.status_code = status.HTTP_200_OK  # nothing new happened
    return UploadResponse(
        status=submission.status, job_id=submission.job_id, document_id=submission.document_id
    )


@router.get("/jobs/{job_id}", responses={404: {"description": "No such job for this tenant"}})
async def get_job(job_id: uuid.UUID, tenant_id: TenantDep, session: SessionDep) -> JobResponse:
    job = await session.scalar(
        select(IngestionJobRecord).where(
            IngestionJobRecord.id == job_id, IngestionJobRecord.tenant_id == tenant_id
        )
    )
    if job is None:
        # Another tenant's job is "not found", not "forbidden": its existence is not revealed.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return JobResponse(
        job_id=job.id,
        status=job.status,
        doc_key=job.doc_key,
        version=job.version,
        attempts=job.attempts,
        error=job.error,
        document_id=job.document_id,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
