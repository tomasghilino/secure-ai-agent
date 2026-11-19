"""The worker and the real queue: ElasticMQ from docker compose (`make up`).

Each test that needs SQS uses its own throwaway queue, so the development queue is never
touched.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import get_embeddings, get_job_queue, get_session, get_storage
from app.core.config import Settings
from app.database.models import IngestionJobRecord
from app.ingestion.jobs import submit_document, sweep_jobs
from app.ingestion.queue import JobRef, QueueMessage, SqsJobQueue, build_sqs_client
from app.ingestion.storage import LocalFileStorage
from app.ingestion.worker import handle_message
from app.main import create_app
from app.rag.embeddings import FakeEmbeddings
from tests.conftest import GeneratedPdfs
from tests.integration.conftest import SUPPORT_SLA, InMemoryJobQueue
from tests.integration.test_documents_api import AMER, form, pdf_file
from tests.integration.test_ingestion_jobs import ModelDown

pytestmark = pytest.mark.integration


@pytest.fixture
def sqs_queue() -> Iterator[SqsJobQueue]:
    client = build_sqs_client(Settings())  # APP_ENV=test: always the local ElasticMQ
    name = f"northstar-test-{uuid.uuid4().hex[:8]}"
    try:
        # Visibility timeout of 1 s: an undeleted message comes back quickly.
        url = client.create_queue(QueueName=name, Attributes={"VisibilityTimeout": "1"})["QueueUrl"]
    except EndpointConnectionError:
        pytest.skip("ElasticMQ not available (run `make up`)")
    yield SqsJobQueue(client, name, wait_seconds=1)
    client.delete_queue(QueueUrl=url)


async def _queued_job(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> QueueMessage:
    metadata, pdf = generated_pdfs[SUPPORT_SLA]
    async with sessions() as session:
        await submit_document(session, storage, queue, pdf, metadata)
    [message] = await queue.receive()
    return message


async def _job(sessions: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> IngestionJobRecord:
    async with sessions() as session:
        return await session.get_one(IngestionJobRecord, job_id)


async def test_a_sent_job_is_received_and_then_deleted(sqs_queue: SqsJobQueue) -> None:
    job = JobRef(uuid.uuid4(), "nsi-amer")
    await sqs_queue.send(job)

    [message] = await sqs_queue.receive()
    assert (message.job, message.receive_count) == (job, 1)
    await sqs_queue.delete(message)

    assert await sqs_queue.receive() == []


async def test_a_message_that_is_not_deleted_is_delivered_again(sqs_queue: SqsJobQueue) -> None:
    await sqs_queue.send(JobRef(uuid.uuid4(), "nsi-amer"))
    [first] = await sqs_queue.receive()

    await asyncio.sleep(1.2)  # the visibility timeout expires: the worker "crashed"
    [again] = await sqs_queue.receive()

    assert again.job == first.job
    assert again.receive_count == 2  # at-least-once delivery, seen from the consumer


async def test_an_invalid_message_is_deleted_without_retrying(
    sessions: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    queue = InMemoryJobQueue()
    message = QueueMessage(job=None, receipt_handle="r", receive_count=1)

    await handle_message(
        message, queue=queue, sessions=sessions, storage=storage, embeddings=FakeEmbeddings()
    )

    assert queue.deleted == [message]


async def test_a_transient_error_keeps_the_message_for_a_retry(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    message = await _queued_job(sessions, storage, queue, generated_pdfs)

    await handle_message(
        message, queue=queue, sessions=sessions, storage=storage, embeddings=ModelDown()
    )

    assert queue.deleted == []  # it will come back after the visibility timeout
    assert message.job is not None
    assert (await _job(sessions, message.job.job_id)).status == "running"


async def test_the_last_attempt_fails_the_job(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    message = await _queued_job(sessions, storage, queue, generated_pdfs)
    last = QueueMessage(message.job, message.receipt_handle, receive_count=3)

    await handle_message(
        last, queue=queue, sessions=sessions, storage=storage, embeddings=ModelDown()
    )

    assert last.job is not None
    job = await _job(sessions, last.job.job_id)
    assert job.status == "failed"
    assert job.error is not None
    assert job.error.startswith("gave up after 3 attempts")


async def test_a_message_with_the_wrong_tenant_finds_no_job(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    message = await _queued_job(sessions, storage, queue, generated_pdfs)
    assert message.job is not None
    forged = QueueMessage(JobRef(message.job.job_id, "nsi-emea"), "r", receive_count=1)

    await handle_message(
        forged, queue=queue, sessions=sessions, storage=storage, embeddings=FakeEmbeddings()
    )

    # Row-Level Security hides the nsi-amer job from an nsi-emea session: nothing to do.
    assert queue.deleted == [forged]
    assert (await _job(sessions, message.job.job_id)).status == "queued"


async def test_the_sweep_resends_lost_jobs_and_fails_abandoned_ones(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    lost = await _queued_job(sessions, storage, queue, generated_pdfs)
    assert lost.job is not None
    async with sessions() as session, session.begin():
        # Pretend the job has waited 10 minutes and its message never arrived.
        await session.execute(
            update(IngestionJobRecord).values(
                updated_at=IngestionJobRecord.updated_at - timedelta(minutes=10)
            )
        )

    async with sessions() as session:
        requeued, abandoned = await sweep_jobs(session, queue)

    assert (requeued, abandoned) == (1, 0)
    assert queue.sent == [lost.job]

    async with sessions() as session, session.begin():
        await session.execute(
            update(IngestionJobRecord).values(
                status="running", updated_at=IngestionJobRecord.updated_at - timedelta(hours=1)
            )
        )
    async with sessions() as session:
        assert await sweep_jobs(session, queue) == (0, 1)
    assert (await _job(sessions, lost.job.job_id)).status == "failed"


async def test_upload_queue_worker_search_end_to_end(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    sqs_queue: SqsJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[SUPPORT_SLA]
    async with sessions() as session:
        app = create_app()
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_storage] = lambda: storage
        app.dependency_overrides[get_job_queue] = lambda: sqs_queue
        app.dependency_overrides[get_embeddings] = lambda: FakeEmbeddings()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Upload: stored and queued, nothing embedded yet.
            upload = await client.post(
                "/v1/documents", headers=AMER, files=pdf_file(pdf), data=form(metadata)
            )
            assert upload.status_code == 202
            job_id = uuid.UUID(upload.json()["job_id"])

            # 2. The worker receives the message from ElasticMQ and processes the job
            #    under the tenant named in the message.
            [message] = await sqs_queue.receive()
            assert message.job == JobRef(job_id, "nsi-amer")
            await handle_message(
                message,
                queue=sqs_queue,
                sessions=sessions,
                storage=storage,
                embeddings=FakeEmbeddings(),
            )
            assert await sqs_queue.receive() == []  # deleted once the job finished

            # 3. The job reports success, and the new document is searchable.
            job = await client.get(f"/v1/jobs/{job_id}", headers=AMER)
            assert job.json()["status"] == "succeeded"
            search = await client.post(
                "/v1/search",
                headers=AMER,
                json={"query": "first response time for critical tickets", "limit": 3},
            )
            assert {hit["doc_key"] for hit in search.json()["results"]} == {"NSI-SLA-CS-002"}
