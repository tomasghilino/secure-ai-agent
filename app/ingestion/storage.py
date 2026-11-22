"""Object storage for uploaded PDFs (ADR-0003): a local directory in development and
tests, an S3 bucket in staging.

Keys are content-addressed (``uploads/<tenant>/<sha256>.pdf``): nothing the client sends
ends up in a path, and storing the same file twice writes the same object.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import boto3
from botocore.exceptions import ClientError

from app.core.config import Settings

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client


class ObjectNotFoundError(LookupError):
    """No object is stored under this key."""


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...


def upload_key(tenant_id: str, sha256: str) -> str:
    return f"uploads/{tenant_id}/{sha256}.pdf"


class LocalFileStorage:
    """Files under a root directory. File I/O blocks, so it runs in a worker thread."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):  # defense in depth against "../" keys
            raise ValueError(f"storage key escapes the storage root: {key!r}")
        return path

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(_write_atomically, self._path(key), data)

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(key) from exc


def _write_atomically(path: Path, data: bytes) -> None:
    # Write a temporary file, then rename it: a reader never sees half a file.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


class S3ObjectStorage:
    """Objects in one S3 bucket. boto3 is synchronous: every call runs in a worker thread.

    Encryption at rest, TLS-only access and blocked public access are properties of the
    bucket (Terraform); the client only sends a checksum so S3 rejects a corrupted upload.
    """

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType="application/pdf",
            ChecksumAlgorithm="SHA256",
        )

    async def get(self, key: str) -> bytes:
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectNotFoundError(key) from exc
            raise  # access denied, throttling...: not a missing file, may be retried
        return await asyncio.to_thread(response["Body"].read)


def build_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "s3" and settings.storage_bucket:
        session = boto3.session.Session(
            profile_name=settings.aws_profile, region_name=settings.aws_region
        )
        return S3ObjectStorage(session.client("s3"), settings.storage_bucket)
    return LocalFileStorage(settings.storage_root)
