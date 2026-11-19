"""POST /v1/documents and GET /v1/jobs/{id} over HTTP (real PostgreSQL, in-memory queue)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_job_queue, get_session, get_storage
from app.core.config import Settings, get_settings
from app.ingestion.pipeline import DocumentMetadata
from app.ingestion.storage import LocalFileStorage
from app.main import create_app
from tests.conftest import GeneratedPdfs
from tests.integration.conftest import RETURNS_V3_0, SUPPORT_SLA, InMemoryJobQueue

pytestmark = pytest.mark.integration

AMER = {"X-Tenant-ID": "nsi-amer"}


def form(metadata: DocumentMetadata) -> dict[str, str]:
    return {
        "doc_key": metadata.doc_key,
        "version": metadata.version,
        "title": metadata.title,
        "effective_date": metadata.effective_date.isoformat(),
        "classification": metadata.classification,
        "status": metadata.status,
    }


def pdf_file(data: bytes, content_type: str = "application/pdf") -> dict[str, Any]:
    return {"file": ("upload.pdf", data, content_type)}


@pytest.fixture
async def client(
    session_with_returns_policies: AsyncSession,
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session_with_returns_policies
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_job_queue] = lambda: queue
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_an_upload_is_accepted_and_its_job_can_be_polled(
    client: httpx.AsyncClient, queue: InMemoryJobQueue, generated_pdfs: GeneratedPdfs
) -> None:
    metadata, pdf = generated_pdfs[SUPPORT_SLA]

    response = await client.post(
        "/v1/documents", headers=AMER, files=pdf_file(pdf), data=form(metadata)
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert response.json()["status"] == "queued"
    assert [str(sent) for sent in queue.sent_ids] == [job_id]

    job = await client.get(f"/v1/jobs/{job_id}", headers=AMER)
    assert job.status_code == 200
    assert (job.json()["status"], job.json()["doc_key"]) == ("queued", "NSI-SLA-CS-002")


async def test_an_indexed_file_is_answered_with_200(
    client: httpx.AsyncClient, generated_pdfs: GeneratedPdfs
) -> None:
    metadata, pdf = generated_pdfs[RETURNS_V3_0]

    response = await client.post(
        "/v1/documents", headers=AMER, files=pdf_file(pdf), data=form(metadata)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "already_indexed"
    assert response.json()["document_id"] is not None


async def test_another_file_for_a_registered_version_is_a_conflict(
    client: httpx.AsyncClient, generated_pdfs: GeneratedPdfs
) -> None:
    metadata, _ = generated_pdfs[RETURNS_V3_0]
    _, other_pdf = generated_pdfs[SUPPORT_SLA]

    response = await client.post(
        "/v1/documents", headers=AMER, files=pdf_file(other_pdf), data=form(metadata)
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    ("content", "content_type"),
    [(b"just text", "application/pdf"), (None, "text/plain")],
    ids=["pdf-content-type-but-not-a-pdf", "pdf-with-wrong-content-type"],
)
async def test_a_file_that_is_not_a_pdf_is_rejected(
    client: httpx.AsyncClient,
    generated_pdfs: GeneratedPdfs,
    content: bytes | None,
    content_type: str,
) -> None:
    metadata, pdf = generated_pdfs[SUPPORT_SLA]

    response = await client.post(
        "/v1/documents",
        headers=AMER,
        files=pdf_file(content or pdf, content_type),
        data=form(metadata),
    )

    assert response.status_code == 415


async def test_a_file_over_the_size_limit_is_rejected(
    client: httpx.AsyncClient, generated_pdfs: GeneratedPdfs
) -> None:
    metadata, pdf = generated_pdfs[SUPPORT_SLA]
    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_settings] = lambda: Settings(max_upload_bytes=len(pdf) - 1)

    response = await client.post(
        "/v1/documents", headers=AMER, files=pdf_file(pdf), data=form(metadata)
    )

    assert response.status_code == 413


@pytest.mark.parametrize(
    "change",
    [{"tenant_id": "nsi-emea"}, {"doc_key": "../../etc"}, {"classification": "secret"}],
    ids=["tenant-in-form", "doc-key-path", "unknown-classification"],
)
async def test_invalid_metadata_is_rejected(
    client: httpx.AsyncClient, generated_pdfs: GeneratedPdfs, change: dict[str, str]
) -> None:
    metadata, pdf = generated_pdfs[SUPPORT_SLA]

    response = await client.post(
        "/v1/documents", headers=AMER, files=pdf_file(pdf), data={**form(metadata), **change}
    )

    assert response.status_code == 422


async def test_another_tenant_cannot_see_the_job(
    client: httpx.AsyncClient, generated_pdfs: GeneratedPdfs
) -> None:
    metadata, pdf = generated_pdfs[SUPPORT_SLA]
    upload = await client.post(
        "/v1/documents", headers=AMER, files=pdf_file(pdf), data=form(metadata)
    )

    response = await client.get(
        f"/v1/jobs/{upload.json()['job_id']}", headers={"X-Tenant-ID": "nsi-emea"}
    )

    assert response.status_code == 404
