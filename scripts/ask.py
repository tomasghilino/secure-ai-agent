"""Ask a question and print a grounded answer with its citations (RAG end to end).

Needs `make ingest` (same embedding model) and, with LLM_PROVIDER=ollama, `make llm-up`.
Usage (repository root):
    uv run python -m scripts.ask "how many days do I have to return a product?"
    uv run python -m scripts.ask "..." --tenant nsi-emea --as-of 2025-06-01
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import time
from datetime import date

from app.core.config import get_settings
from app.database.session import build_engine, build_session_factory
from app.database.tenancy import tenant_session
from app.rag.answer import answer_question
from app.rag.embeddings import build_embedding_provider
from app.rag.llm import build_chat_model
from app.rag.retrieval import SearchFilters


async def main(question: str, tenant_id: str, as_of: date | None) -> None:
    settings = get_settings()
    embeddings = build_embedding_provider(settings)
    chat_model = build_chat_model(settings)
    print(f"Question: {question}")
    print(f"Tenant:   {tenant_id} | chat: {settings.llm_provider} | {embeddings.model_id}\n")

    engine = build_engine(settings.database_url)
    started = time.perf_counter()
    try:
        # A tenant session, exactly like an API request (Row-Level Security applies).
        async with tenant_session(build_session_factory(engine), tenant_id) as session:
            answer = await answer_question(
                session,
                embeddings,
                chat_model,
                question,
                tenant_id=tenant_id,
                filters=SearchFilters(as_of=as_of),
                mode=settings.rag_retrieval_mode,
                top_k=settings.rag_top_k,
                max_context_tokens=settings.rag_max_context_tokens,
            )
    finally:
        await engine.dispose()
    elapsed = time.perf_counter() - started

    print(answer.text, end="\n\n")
    if answer.reason:
        print(f"(no answer: {answer.reason}; {answer.sources_given} sources given)")
    for citation in answer.citations:
        print(
            f"[{citation.number}] {citation.title} ({citation.doc_key} v{citation.version}), "
            f"p.{citation.page_number}, {citation.section}"
        )
    print(f"\n{elapsed:.1f} s | usage: {answer.usage}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question")
    parser.add_argument("--tenant", default="nsi-amer")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(args.question, args.tenant, args.as_of), loop_factory=loop_factory)
