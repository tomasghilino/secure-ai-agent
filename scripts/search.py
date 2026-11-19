"""Search the chunks for a question and print the best matches with their scores.

Needs `make ingest` first, with the same embedding model. Usage (repository root):
    uv run python -m scripts.search "how many days do I have to return a product?"
    uv run python -m scripts.search "NX-4400B warranty" --mode keyword
    uv run python -m scripts.search "..." --tenant nsi-emea --limit 10 --as-of 2025-06-01
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import textwrap
from datetime import date

from app.core.config import get_settings
from app.database.session import build_engine, build_session_factory
from app.rag.embeddings import build_embedding_provider
from app.rag.retrieval import DEFAULT_LIMIT, Mode, SearchFilters, search_chunks

SNIPPET_CHARS = 240


async def main(query: str, tenant_id: str, limit: int, mode: Mode, as_of: date | None) -> None:
    settings = get_settings()
    embeddings = build_embedding_provider(settings)
    print(f"Query:  {query}")
    print(f"Tenant: {tenant_id} | mode: {mode} | model: {embeddings.model_id}")
    print(f"As of:  {as_of or 'today (active versions)'}\n")

    engine = build_engine(settings.database_url)
    try:
        async with build_session_factory(engine)() as session:
            hits = await search_chunks(
                session,
                embeddings,
                query,
                tenant_id=tenant_id,
                limit=limit,
                mode=mode,
                filters=SearchFilters(as_of=as_of),
            )
    finally:
        await engine.dispose()

    if not hits:
        print("No chunks found: run `make ingest` with this embedding model first.")
    for rank, hit in enumerate(hits, start=1):
        body = hit.content.split("\n\n", 1)[-1]  # drop the heading path, printed above
        similarity = "-" if hit.similarity is None else f"{hit.similarity:.3f}"
        print(
            f"{rank}. score {hit.score:.4f} (similarity {similarity}) | {hit.doc_key} "
            f"v{hit.version} p.{hit.page_number}\n   {' > '.join(hit.section_path)}"
        )
        print(textwrap.indent(textwrap.shorten(body, SNIPPET_CHARS), "   "), end="\n\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query")
    parser.add_argument("--tenant", default="nsi-amer")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--mode", choices=["dense", "keyword", "hybrid"], default="hybrid")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(
        main(args.query, args.tenant, args.limit, args.mode, args.as_of),
        loop_factory=loop_factory,
    )
