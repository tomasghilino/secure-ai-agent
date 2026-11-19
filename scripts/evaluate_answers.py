"""Run every evaluation question through RAG (hybrid retrieval + the chat model) and check
the answers: answered vs refused, cited sections, outdated versions, reference numbers.

Needs `make ingest`; with LLM_PROVIDER=ollama also `make llm-up` (free). Usage:
    uv run python -m scripts.evaluate_answers
Prints a summary and every non-passing question; details go to reports/ (not committed).
"""

from __future__ import annotations

import asyncio
import io
import json
import statistics
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.database.session import build_engine, build_session_factory
from app.evaluation.answers import evaluate_answers
from app.evaluation.dataset import load_dataset
from app.rag.embeddings import build_embedding_provider
from app.rag.llm import build_chat_model

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


def share(part: int, total: int) -> str:
    return f"{part}/{total} ({part / total:.0%})" if total else "-"


async def main() -> None:
    settings = get_settings()
    embeddings = build_embedding_provider(settings)
    chat_model = build_chat_model(settings)
    questions = load_dataset()
    engine = build_engine(settings.database_url)
    try:
        checks = await evaluate_answers(
            build_session_factory(engine),
            embeddings,
            chat_model,
            questions,
            mode=settings.rag_retrieval_mode,
            top_k=settings.rag_top_k,
            max_context_tokens=settings.rag_max_context_tokens,
        )
    finally:
        await engine.dispose()

    answerable = [c for c in checks if c.answerable]
    unanswerable = [c for c in checks if not c.answerable]
    answered = [c for c in answerable if c.status == "answered"]
    with_numbers = [c for c in answered if c.numbers_match is not None]
    model = getattr(chat_model, "model", None) or getattr(chat_model, "model_id", "fake")
    print(f"Chat model: {settings.llm_provider}/{model} | embeddings: {embeddings.model_id}")
    print(f"Retrieval: {settings.rag_retrieval_mode}, top {settings.rag_top_k}")
    print(f"Reasoning: {settings.llm_reasoning}")
    print(f"Questions: {len(answerable)} answerable, {len(unanswerable)} unanswerable\n")
    print("| Check | Result |")
    print("| --- | --- |")
    print(f"| Answerable questions answered | {share(len(answered), len(answerable))} |")
    print(
        "| Answers citing a relevant section | "
        f"{share(sum(c.cited_relevant for c in answered), len(answered))} |"
    )
    print(
        "| Answers containing every reference number | "
        f"{share(sum(bool(c.numbers_match) for c in with_numbers), len(with_numbers))} |"
    )
    print(
        "| Answers citing a forbidden version | "
        f"{share(sum(c.cited_forbidden for c in answered), len(answered))} |"
    )
    refused = sum(c.status == "insufficient_evidence" for c in unanswerable)
    print(f"| Unanswerable questions refused | {share(refused, len(unanswerable))} |")
    seconds = [c.seconds for c in checks]
    print(f"| Median latency (s) | {statistics.median(seconds):.1f} |")
    print(f"| Output tokens (total) | {sum(c.output_tokens for c in checks)} |")

    print("\nNot passing:")
    for c in checks:
        failed = (
            (c.answerable and (c.status != "answered" or not c.cited_relevant))
            or (c.answerable and c.numbers_match is False)
            or c.cited_forbidden
            or (not c.answerable and c.status == "answered")
        )
        if failed:
            print(f"- {c.question_id} [{c.category}] {c.status} {c.reason or ''}: {c.text[:160]}")

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"answers-{stamp}.json"
    path.write_text(json.dumps([asdict(c) for c in checks], indent=2), encoding="utf-8")
    print(f"\nDetails: {path.relative_to(REPORTS_DIR.parent)}")


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
