"""Dense, keyword and hybrid search against a real PostgreSQL (fake embeddings: word overlap)."""

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.embeddings import FakeEmbeddings
from app.rag.retrieval import SearchFilters, search_chunks
from tests.integration.conftest import RETURNS_EMEA, RETURNS_V2_1, RETURNS_V3_0

pytestmark = pytest.mark.integration


class OtherModel(FakeEmbeddings):
    @property
    def model_id(self) -> str:
        return "another-model"


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
        mode="dense",
    )

    assert len(hits) == 5
    assert hits[0].section_path == ("Returns and Refunds Policy", "3. Return Eligibility")
    assert hits[0].page_number == 1
    similarities = [hit.similarity for hit in hits]
    assert similarities == sorted(similarities, reverse=True)  # type: ignore[type-var]


async def test_vectors_from_another_model_are_never_compared(
    session_with_returns_policies: AsyncSession,
) -> None:
    hits = await search_chunks(
        session_with_returns_policies, OtherModel(), "return", tenant_id="nsi-amer", mode="dense"
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
        mode="dense",
    )

    assert len(hits) == 8  # every EMEA chunk, not only those among the first candidates


async def test_keyword_search_matches_an_exact_product_code(
    session_with_returns_policies: AsyncSession,
) -> None:
    hits = await search_chunks(
        session_with_returns_policies,
        FakeEmbeddings(),
        "is the NX-6200 returnable?",
        tenant_id="nsi-amer",
        mode="keyword",
    )

    assert hits
    assert all("NX-6200" in hit.content for hit in hits[:2])
    assert {(hit.doc_key, hit.version) for hit in hits} == {RETURNS_V3_0}  # filters apply too


async def test_hybrid_search_fuses_both_rankings(
    session_with_returns_policies: AsyncSession,
) -> None:
    hits = await search_chunks(
        session_with_returns_policies,
        FakeEmbeddings(),
        "is the NX-6200 returnable?",
        tenant_id="nsi-amer",
        limit=20,
    )

    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(0 < score <= 2 / 61 for score in scores)  # RRF: at most 1/61 per ranking
    assert any(hit.similarity is not None for hit in hits)


async def test_a_point_in_time_search_returns_the_version_in_force_then(
    session_with_returns_policies: AsyncSession,
) -> None:
    # v2.1 took effect on 2024-02-01 and v3.0 on 2026-03-01.
    hits = await search_chunks(
        session_with_returns_policies,
        FakeEmbeddings(),
        "When must a return request be submitted?",
        tenant_id="nsi-amer",
        limit=100,
        filters=SearchFilters(as_of=date(2025, 6, 1)),
    )

    assert {(hit.doc_key, hit.version) for hit in hits} == {RETURNS_V2_1}


async def test_nothing_was_in_force_before_the_first_version(
    session_with_returns_policies: AsyncSession,
) -> None:
    hits = await search_chunks(
        session_with_returns_policies,
        FakeEmbeddings(),
        "returns",
        tenant_id="nsi-amer",
        filters=SearchFilters(as_of=date(2020, 1, 1)),
    )

    assert hits == []


async def test_a_classification_filter_excludes_other_levels(
    session_with_returns_policies: AsyncSession,
) -> None:
    hits = await search_chunks(
        session_with_returns_policies,
        FakeEmbeddings(),
        "returns",
        tenant_id="nsi-amer",
        filters=SearchFilters(classifications=("public",)),  # the policies are "internal"
    )

    assert hits == []
