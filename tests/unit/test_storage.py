from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.ingestion.storage import (
    LocalFileStorage,
    ObjectNotFoundError,
    S3ObjectStorage,
    upload_key,
)

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client


async def test_a_stored_object_can_be_read_back(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    key = upload_key("nsi-amer", "ab" * 32)

    await storage.put(key, b"%PDF-1.7 first")
    await storage.put(key, b"%PDF-1.7 second")  # same key again: replaced, never appended

    assert await storage.get(key) == b"%PDF-1.7 second"
    assert [p.name for p in (tmp_path / "uploads" / "nsi-amer").iterdir()] == [f"{'ab' * 32}.pdf"]


async def test_a_missing_object_raises_object_not_found(tmp_path: Path) -> None:
    with pytest.raises(ObjectNotFoundError):
        await LocalFileStorage(tmp_path).get("uploads/nsi-amer/missing.pdf")


async def test_a_key_cannot_escape_the_storage_root(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path / "root")

    with pytest.raises(ValueError, match="escapes"):
        await storage.put("../outside.pdf", b"%PDF-")


# ----------------------------------------------------------- S3 (stubbed, no network)


def s3_client() -> S3Client:
    # Unsigned: the stubber answers every call, so no credentials are looked up or needed.
    session = boto3.session.Session(region_name="us-east-1")
    return session.client("s3", config=Config(signature_version=UNSIGNED))


async def test_s3_stores_and_reads_back_an_object() -> None:
    client = s3_client()
    key = upload_key("nsi-amer", "cd" * 32)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "docs",
                "Key": key,
                "Body": b"%PDF-1.7",
                "ContentType": "application/pdf",
                "ChecksumAlgorithm": "SHA256",
            },
        )
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(b"%PDF-1.7"), 8)},
            {"Bucket": "docs", "Key": key},
        )
        storage = S3ObjectStorage(client, "docs")

        await storage.put(key, b"%PDF-1.7")
        assert await storage.get(key) == b"%PDF-1.7"
        stub.assert_no_pending_responses()


async def test_s3_maps_a_missing_key_to_object_not_found() -> None:
    client = s3_client()
    with Stubber(client) as stub:
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

        with pytest.raises(ObjectNotFoundError):
            await S3ObjectStorage(client, "docs").get("uploads/nsi-amer/missing.pdf")


async def test_s3_access_denied_is_not_a_missing_object() -> None:
    # Not found is a permanent ingestion error; a permission problem must stay visible.
    client = s3_client()
    with Stubber(client) as stub:
        stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)

        with pytest.raises(ClientError):
            await S3ObjectStorage(client, "docs").get("uploads/nsi-amer/x.pdf")
