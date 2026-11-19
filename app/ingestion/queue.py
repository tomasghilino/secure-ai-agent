"""The ingestion job queue: the SQS API, served by ElasticMQ locally (ADR-0003).

Messages only carry a job reference (job id + tenant); the job itself lives in
PostgreSQL. The worker processes each job under that tenant (Row-Level Security), so a
message naming the wrong tenant finds no job. SQS delivers each message *at least once*,
so whoever processes it must tolerate duplicates.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import boto3
import botocore.session
from botocore import UNSIGNED
from botocore.config import Config

from app.core.config import Settings

if TYPE_CHECKING:
    from types_boto3_sqs.client import SQSClient


_TENANT_ID = re.compile(r"^[a-z0-9-]{1,64}$")


@dataclass(frozen=True, slots=True)
class JobRef:
    job_id: uuid.UUID
    tenant_id: str


@dataclass(frozen=True, slots=True)
class QueueMessage:
    job: JobRef | None  # None when the body is not a valid job message
    receipt_handle: str  # needed to delete this delivery of the message
    receive_count: int  # 1 on the first delivery; grows with every retry


class JobQueue(Protocol):
    async def send(self, job: JobRef) -> None: ...

    async def receive(self) -> list[QueueMessage]: ...

    async def delete(self, message: QueueMessage) -> None: ...


def encode_job(job: JobRef) -> str:
    return json.dumps({"job_id": str(job.job_id), "tenant_id": job.tenant_id})


def decode_job(body: str) -> JobRef | None:
    try:
        payload = json.loads(body)
        job_id, tenant_id = uuid.UUID(payload["job_id"]), payload["tenant_id"]
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(tenant_id, str) or not _TENANT_ID.match(tenant_id):
        return None
    return JobRef(job_id, tenant_id)


class SqsJobQueue:
    """boto3 is synchronous: every call runs in a worker thread to keep the event loop free."""

    def __init__(self, client: SQSClient, queue_name: str, *, wait_seconds: int = 10) -> None:
        self._client = client
        self._queue_name = queue_name
        self._wait_seconds = wait_seconds
        self._queue_url: str | None = None

    async def _url(self) -> str:
        # Resolved on first use, not at startup: the API can start before the queue is up.
        if self._queue_url is None:
            response = await asyncio.to_thread(
                self._client.get_queue_url, QueueName=self._queue_name
            )
            self._queue_url = response["QueueUrl"]
        return self._queue_url

    async def send(self, job: JobRef) -> None:
        await asyncio.to_thread(
            self._client.send_message, QueueUrl=await self._url(), MessageBody=encode_job(job)
        )

    async def receive(self) -> list[QueueMessage]:
        # Long polling: waits up to wait_seconds for a message instead of returning at once.
        response = await asyncio.to_thread(
            self._client.receive_message,
            QueueUrl=await self._url(),
            MaxNumberOfMessages=1,
            WaitTimeSeconds=self._wait_seconds,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        return [
            QueueMessage(
                job=decode_job(message["Body"]),
                receipt_handle=message["ReceiptHandle"],
                receive_count=int(message["Attributes"]["ApproximateReceiveCount"]),
            )
            for message in response.get("Messages", [])
        ]

    async def delete(self, message: QueueMessage) -> None:
        await asyncio.to_thread(
            self._client.delete_message,
            QueueUrl=await self._url(),
            ReceiptHandle=message.receipt_handle,
        )


def build_sqs_client(settings: Settings) -> SQSClient:
    if settings.sqs_endpoint_url is None:  # real SQS (staging): the standard credential chain
        session = boto3.session.Session(
            profile_name=settings.aws_profile, region_name=settings.aws_region
        )
        return session.client("sqs")
    # ElasticMQ checks no credentials, so requests are not signed. botocore must also not
    # look up AWS_PROFILE: inside a container that profile does not exist and would fail.
    core = botocore.session.Session(session_vars={"profile": (None, None, None, None)})
    session = boto3.session.Session(botocore_session=core, region_name=settings.aws_region)
    return session.client(
        "sqs",
        endpoint_url=settings.sqs_endpoint_url,
        config=Config(signature_version=UNSIGNED),
    )


def build_job_queue(settings: Settings) -> JobQueue:
    return SqsJobQueue(build_sqs_client(settings), settings.ingestion_queue_name)
