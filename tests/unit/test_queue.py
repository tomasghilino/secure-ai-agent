import uuid

import pytest

from app.ingestion.queue import JobRef, decode_job, encode_job


def test_a_job_message_round_trips() -> None:
    job = JobRef(uuid.uuid4(), "nsi-amer")

    assert decode_job(encode_job(job)) == job


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"job": "x"}',
        '{"job_id": "not-a-uuid", "tenant_id": "nsi-amer"}',
        '{"job_id": "6f1c2d4e-8a3b-4c5d-9e7f-0a1b2c3d4e5f"}',
        '{"job_id": "6f1c2d4e-8a3b-4c5d-9e7f-0a1b2c3d4e5f", "tenant_id": "../NSI AMER"}',
    ],
    ids=["not-json", "not-an-object", "no-job-id", "bad-uuid", "no-tenant", "bad-tenant"],
)
def test_an_invalid_body_decodes_to_none(body: str) -> None:
    assert decode_job(body) is None
