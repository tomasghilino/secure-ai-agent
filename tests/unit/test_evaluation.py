import uuid

from app.evaluation.answers import check_answer, numbers_in
from app.evaluation.dataset import Question, SectionRef, VersionRef, load_dataset
from app.evaluation.retrieval import score_question, summarize
from app.rag.answer import Answer, Citation
from app.rag.retrieval import SearchHit

QUESTION = Question(
    id="q01",
    tenant="nsi-amer",
    category="factual",
    question="How long does a customer have to ask for a return?",
    relevant=[SectionRef(doc_key="RET", version="3.0", section="3. Return Eligibility")],
    forbidden=[VersionRef(doc_key="RET", version="2.1")],
    answer="30 calendar days from delivery.",
)


def hit(version: str, section: str) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        doc_key="RET",
        version=version,
        title="Returns",
        classification="internal",
        page_number=1,
        section_path=("Returns", section),
        content="...",
        score=1.0,
    )


def test_the_position_of_the_first_relevant_hit_is_recorded() -> None:
    hits = [hit("3.0", "1. Scope"), hit("3.0", "3. Return Eligibility")]

    score = score_question(QUESTION, "hybrid", hits)

    assert (score.first_relevant, score.recall_at_5, score.forbidden) == (2, 1.0, False)


def test_a_forbidden_version_anywhere_in_the_top_10_is_flagged() -> None:
    hits = [hit("3.0", "3. Return Eligibility"), hit("2.1", "2. Return Eligibility")]

    assert score_question(QUESTION, "dense", hits).forbidden


def test_summary_metrics() -> None:
    found_first = score_question(QUESTION, "dense", [hit("3.0", "3. Return Eligibility")])
    found_third = score_question(
        QUESTION, "dense", [hit("3.0", "x"), hit("3.0", "y"), hit("3.0", "3. Return Eligibility")]
    )
    missed = score_question(QUESTION, "dense", [hit("3.0", "x")])

    summary = summarize([found_first, found_third, missed])

    assert (summary.hit_at_1, summary.hit_at_3) == (1 / 3, 2 / 3)
    assert summary.mrr_at_10 == (1 + 1 / 3 + 0) / 3


def test_an_answer_is_checked_against_the_labels() -> None:
    citation = Citation(1, "RET", "3.0", "Returns", 1, "3. Return Eligibility")
    answer = Answer("answered", "You have 30 calendar days [1].", [citation])

    check = check_answer(QUESTION, answer, seconds=1.0)

    assert (check.cited_relevant, check.cited_forbidden, check.numbers_match) == (
        True,
        False,
        True,
    )
    assert numbers_in("USD 50,000.01 to 250,000") == {"50000.01", "250000"}


def test_the_dataset_loads_and_covers_every_category() -> None:
    questions = load_dataset()

    assert len(questions) >= 30
    assert len({q.id for q in questions}) == len(questions)
    assert {q.category for q in questions} == {
        "factual",
        "exact-code",
        "cross-lingual",
        "outdated-trap",
        "point-in-time",
        "other-tenant",
        "unanswerable",
    }
