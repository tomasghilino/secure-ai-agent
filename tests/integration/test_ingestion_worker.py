"""The worker and the real queue: ElasticMQ from docker compose (`make up`).

Each test uses its own throwaway queue, so the development queue is never touched.
"""

import asyncio
import uuid
from collections.abc import Iterator

import httpx
import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import get_embeddings, get_job_queue, get_session, get_storage
from app.core.config import Settings
from app.ingestion.jobs import submit_document
from app.ingestion.queue import QueueMessage, SqsJobQueue, build_sqs_client
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


async def test_a_sent_job_is_received_and_then_deleted(sqs_queue: SqsJobQueue) -> None:
    job_id = uuid.uuid4()
    await sqs_queue.send(job_id)

    [message] = await sqs_queue.receive()
    assert (message.job_id, message.receive_count) == (job_id, 1)
    await sqs_queue.delete(message)

    assert await sqs_queue.receive() == []


async def test_a_message_that_is_not_deleted_is_delivered_again(sqs_queue: SqsJobQueue) -> None:
    await sqs_queue.send(uuid.uuid4())
    [first] = await sqs_queue.receive()

    await asyncio.sleep(1.2)  # the visibility timeout expires: the worker "crashed"
    [again] = await sqs_queue.receive()

    assert again.job_id == first.job_id
    assert again.receive_count == 2  # at-least-once delivery, seen from the consumer


async def test_an_invalid_message_is_deleted_without_retrying(
    sessions: async_sessionmaker[AsyncSession], storage: LocalFileStorage
) -> None:
    queue = InMemoryJobQueue()
    message = QueueMessage(job_id=None, receipt_handle="r", receive_count=1)

    await handle_message(
        message, queue=queue, sessions=sessions, storage=storage, embeddings=FakeEmbeddings()
    )

    assert queue.deleted == [message]


async def test_a_job_that_hits_a_transient_error_keeps_its_message(
    sessions: async_sessionmaker[AsyncSession],
    storage: LocalFileStorage,
    queue: InMemoryJobQueue,
    generated_pdfs: GeneratedPdfs,
) -> None:
    metadata, pdf = generated_pdfs[SUPPORT_SLA]
    async with sessions() as session:
        await submit_document(session, storage, queue, pdf, metadata)
    [message] = await queue.receive()

    await handle_message(
        message, queue=queue, sessions=sessions, storage=storage, embeddings=ModelDown()
    )

    assert queue.deleted == []  # it will come back after the visibility timeout


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

            # 2. The worker receives the message from ElasticMQ and processes the job.
            [message] = await sqs_queue.receive()
            assert message.job_id == job_id
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
