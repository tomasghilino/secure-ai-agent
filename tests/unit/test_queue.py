import uuid

import pytest

from app.ingestion.queue import decode_job, encode_job


def test_a_job_message_round_trips() -> None:
    job_id = uuid.uuid4()

    assert decode_job(encode_job(job_id)) == job_id


@pytest.mark.parametrize("body", ["not json", "[]", '{"job": "x"}', '{"job_id": "not-a-uuid"}'])
def test_an_invalid_body_decodes_to_none(body: str) -> None:
    assert decode_job(body) is None
