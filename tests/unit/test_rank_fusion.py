import uuid

from app.rag.retrieval import SearchHit, reciprocal_rank_fusion


def hit(name: str, similarity: float | None = None) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        doc_key=name,
        version="1.0",
        title=name,
        classification="internal",
        page_number=1,
        section_path=(name,),
        content=name,
        score=0.0,
        similarity=similarity,
    )


def test_a_chunk_ranked_well_by_both_methods_wins() -> None:
    dense = [hit("a", 0.9), hit("b", 0.8), hit("c", 0.7)]
    keyword = [hit("c"), hit("d"), hit("b")]

    fused = reciprocal_rank_fusion([dense, keyword], k=60)

    # b: 1/62 + 1/63 and c: 1/63 + 1/61 beat a (1/61 only) and d (1/62 only).
    assert [h.doc_key for h in fused] == ["c", "b", "a", "d"]
    assert fused[0].score == 1 / 63 + 1 / 61


def test_the_dense_similarity_survives_fusion() -> None:
    fused = reciprocal_rank_fusion([[hit("a", 0.9)], [hit("a")]])

    assert fused[0].similarity == 0.9


def test_a_smaller_k_rewards_the_top_positions_more() -> None:
    dense = [hit("a"), hit("b")]
    keyword = [hit("b"), hit("c")]

    assert reciprocal_rank_fusion([dense, keyword], k=1)[0].doc_key == "b"
    assert reciprocal_rank_fusion([[hit("a")], []], k=1)[0].score == 0.5


def test_a_lighter_ranking_cannot_outvote_a_strong_hit_alone() -> None:
    dense = [hit("a"), hit("b")]
    keyword = [hit("b"), hit("c")]

    equal = reciprocal_rank_fusion([dense, keyword], k=60)
    weighted = reciprocal_rank_fusion([dense, keyword], k=60, weights=[1.0, 0.5])

    assert [h.doc_key for h in equal] == ["b", "a", "c"]
    assert weighted[2].doc_key == "c"
    assert weighted[2].score == 0.5 / 62
