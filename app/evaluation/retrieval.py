"""Retrieval metrics: is the evidence found, and how high is it ranked?

Measured separately from generation: if the right section is not retrieved, no prompt can
fix the answer. All metrics are deterministic (no model judges anything).

- hit@k       the question has at least one relevant section in the top k
- recall@5    share of the question's relevant sections that are in the top 5
- MRR@10      1 / position of the first relevant section (0 if not in the top 10)
- forbidden   a forbidden version (outdated, other tenant) appears in the top 10.
              Must be 0: the filters are SQL, so this checks the filters, not the model.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.tenancy import tenant_session
from app.evaluation.dataset import Question
from app.rag.embeddings import EmbeddingProvider
from app.rag.retrieval import Mode, SearchFilters, SearchHit, search_chunks

DEPTH = 10  # hits retrieved per question


@dataclass(frozen=True, slots=True)
class QuestionScore:
    question_id: str
    category: str
    mode: Mode
    first_relevant: int | None  # 1-based position of the first relevant hit
    recall_at_5: float
    forbidden: bool


def score_question(question: Question, mode: Mode, hits: Sequence[SearchHit]) -> QuestionScore:
    positions = [
        position
        for position, hit in enumerate(hits[:DEPTH], start=1)
        if any(ref.matches(hit) for ref in question.relevant)
    ]
    found = sum(1 for ref in question.relevant if any(ref.matches(hit) for hit in hits[:5]))
    return QuestionScore(
        question_id=question.id,
        category=question.category,
        mode=mode,
        first_relevant=positions[0] if positions else None,
        recall_at_5=found / len(question.relevant) if question.relevant else 0.0,
        forbidden=any(ref.matches(hit) for ref in question.forbidden for hit in hits[:DEPTH]),
    )


@dataclass(frozen=True, slots=True)
class Summary:
    questions: int
    hit_at_1: float
    hit_at_3: float
    hit_at_5: float
    recall_at_5: float
    mrr_at_10: float
    forbidden: int


def summarize(scores: Sequence[QuestionScore]) -> Summary:
    n = len(scores)

    def share(k: int) -> float:
        return sum(1 for s in scores if s.first_relevant and s.first_relevant <= k) / n

    return Summary(
        questions=n,
        hit_at_1=share(1),
        hit_at_3=share(3),
        hit_at_5=share(5),
        recall_at_5=sum(s.recall_at_5 for s in scores) / n,
        mrr_at_10=sum(1 / s.first_relevant for s in scores if s.first_relevant) / n,
        forbidden=sum(1 for s in scores if s.forbidden),
    )


async def evaluate_retrieval(
    sessions: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingProvider,
    questions: Sequence[Question],
    modes: Sequence[Mode] = ("dense", "keyword", "hybrid"),
) -> list[QuestionScore]:
    """Score every answerable question in every mode (unanswerable ones have no target)."""
    scores: list[QuestionScore] = []
    for question in (q for q in questions if q.answerable):
        for mode in modes:
            async with tenant_session(sessions, question.tenant) as session:
                hits = await search_chunks(
                    session,
                    embeddings,
                    question.question,
                    tenant_id=question.tenant,
                    limit=DEPTH,
                    mode=mode,
                    filters=SearchFilters(as_of=question.as_of),
                )
            scores.append(score_question(question, mode, hits))
    return scores


def by_mode_and_category(
    scores: Sequence[QuestionScore],
) -> dict[tuple[str, str], list[QuestionScore]]:
    groups: dict[tuple[str, str], list[QuestionScore]] = defaultdict(list)
    for score in scores:
        groups[score.mode, "all"].append(score)
        groups[score.mode, score.category].append(score)
    return groups
