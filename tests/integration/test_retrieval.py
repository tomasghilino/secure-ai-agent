"""Semantic search against a real PostgreSQL, with fake embeddings (word overlap)."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.pipeline import ingest_pdf
from app.rag.embeddings import FakeEmbeddings
from app.rag.retrieval import search_chunks
from tests.conftest import GeneratedPdfs

pytestmark = pytest.mark.integration

RETURNS_V2_1 = ("NSI-POL-RET-003", "2.1")  # nsi-amer, superseded (45-day window)
RETURNS_V3_0 = ("NSI-POL-RET-003", "3.0")  # nsi-amer, active (30-day window)
RETURNS_EMEA = ("NSI-EU-POL-RET-001", "1.2")  # nsi-emea, active (21-day window)


class OtherModel(FakeEmbeddings):
    @property
    def model_id(self) -> str:
        return "another-model"


@pytest.fixture
async def session_with_returns_policies(
    session: AsyncSession, generated_pdfs: GeneratedPdfs
) -> AsyncSession:
    for key in (RETURNS_V2_1, RETURNS_V3_0, RETURNS_EMEA):
        metadata, data = generated_pdfs[key]
        await ingest_pdf(session, data, metadata, FakeEmbeddings())
    return session


@pytest.mark.parametrize(
    ("tenant_id", "expected"), [("nsi-amer", RETURNS_V3_0), ("nsi-emea", RETURNS_EMEA)]
)
async def test_only_the_active_version_of_the_tenant_is_searchable(
    session_with_returns_policies: AsyncSession, tenant_id: str, expected: tuple[str, str]
) -> None:
    hits = await search_chunks(
        session_with_returns_policies,
        FakeEmbeddings(),
        "return request calendar days",
        tenant_id=tenant_id,
        limit=100,  # more than all the stored chunks: nothing is cut by the limit
    )

    assert hits
    assert {(hit.doc_key, hit.version) for hit in hits} == {expected}


async def test_the_closest_chunk_comes_first(session_with_returns_policies: AsyncSession) -> None:
    hits = await search_chunks(
        session_with_returns_policies,
        FakeEmbeddings(),
        "When must a return request be submitted?",
        tenant_id="nsi-amer",
    )

    assert len(hits) == 5
    assert hits[0].section_path == ("Returns and Refunds Policy", "3. Return Eligibility")
    assert hits[0].page_number == 1
    distances = [hit.distance for hit in hits]
    assert distances == sorted(distances)


async def test_vectors_from_another_model_are_never_compared(
    session_with_returns_policies: AsyncSession,
) -> None:
    hits = await search_chunks(
        session_with_returns_policies, OtherModel(), "return", tenant_id="nsi-amer"
    )

    assert hits == []


async def test_a_filtered_index_scan_still_finds_every_match(
    session_with_returns_policies: AsyncSession,
) -> None:
    session = session_with_returns_policies
    # Force the HNSW index (on a tiny table the planner prefers to sort every row) and let
    # one index pass return only 10 candidates. The query is about the AMER policy, so the
    # nearest candidates are AMER chunks, and the EMEA filter runs *after* the index.
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    await session.execute(text("SET LOCAL enable_sort = off"))
    await session.execute(text("SET LOCAL hnsw.ef_search = 10"))

    hits = await search_chunks(
        session,
        FakeEmbeddings(),
        "Director of Customer Operations strategic accounts late return 60 calendar days",
        tenant_id="nsi-emea",
        limit=100,
    )

    assert len(hits) == 8  # every EMEA chunk, not only those among the first candidates
