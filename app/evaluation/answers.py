"""Answer checks: does the assistant answer when it should, refuse when it should, and
cite the right section? Deterministic checks only; judging wording with a model (Ragas
faithfulness / relevance) is a separate, paid step (Day 4).

Per question:
- answerable   answered? cited a relevant section? cited a forbidden version?
               the numbers of the reference answer all appear in the answer?
- unanswerable refused (``insufficient_evidence``)?
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.tenancy import tenant_session
from app.evaluation.dataset import Question
from app.rag.answer import Answer, answer_question
from app.rag.embeddings import EmbeddingProvider
from app.rag.retrieval import Mode, SearchFilters

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


@dataclass(frozen=True, slots=True)
class AnswerCheck:
    question_id: str
    category: str
    answerable: bool
    status: str
    reason: str | None
    cited_relevant: bool  # at least one citation points to a relevant section
    cited_forbidden: bool  # a citation points to a forbidden version
    numbers_match: bool | None  # None when the reference answer has no numbers
    seconds: float
    output_tokens: int
    text: str


def check_answer(question: Question, answer: Answer, seconds: float) -> AnswerCheck:
    relevant = {(r.doc_key, r.version, r.section) for r in question.relevant}
    forbidden = {(r.doc_key, r.version) for r in question.forbidden}
    expected_numbers = numbers_in(question.answer or "")
    return AnswerCheck(
        question_id=question.id,
        category=question.category,
        answerable=question.answerable,
        status=answer.status,
        reason=answer.reason,
        cited_relevant=any((c.doc_key, c.version, c.section) in relevant for c in answer.citations),
        cited_forbidden=any((c.doc_key, c.version) in forbidden for c in answer.citations),
        numbers_match=(
            expected_numbers <= numbers_in(answer.text)
            if expected_numbers and answer.status == "answered"
            else None
        ),
        seconds=seconds,
        output_tokens=int((answer.usage or {}).get("output_tokens", 0)),
        text=answer.text,
    )


async def evaluate_answers(
    sessions: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingProvider,
    chat_model: BaseChatModel,
    questions: Sequence[Question],
    *,
    mode: Mode,
    top_k: int,
    max_context_tokens: int,
) -> list[AnswerCheck]:
    checks: list[AnswerCheck] = []
    for question in questions:
        started = time.perf_counter()
        async with tenant_session(sessions, question.tenant) as session:
            answer = await answer_question(
                session,
                embeddings,
                chat_model,
                question.question,
                tenant_id=question.tenant,
                filters=SearchFilters(as_of=question.as_of),
                mode=mode,
                top_k=top_k,
                max_context_tokens=max_context_tokens,
            )
        checks.append(check_answer(question, answer, time.perf_counter() - started))
    return checks
