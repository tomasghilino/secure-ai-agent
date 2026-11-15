"""Ask a question and print the most similar chunks, with their distances.

Needs `make ingest` first, with the same embedding model. Usage (repository root):
    uv run python -m scripts.search "how many days do I have to return a product?"
    uv run python -m scripts.search "..." --tenant nsi-emea --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import textwrap

from app.core.config import get_settings
from app.database.session import build_engine, build_session_factory
from app.rag.embeddings import build_embedding_provider
from app.rag.retrieval import DEFAULT_LIMIT, search_chunks

SNIPPET_CHARS = 240


async def main(query: str, tenant_id: str, limit: int) -> None:
    settings = get_settings()
    embeddings = build_embedding_provider(settings)
    print(f"Query:  {query}")
    print(f"Tenant: {tenant_id} | model: {embeddings.model_id}\n")

    engine = build_engine(settings.database_url)
    try:
        async with build_session_factory(engine)() as session:
            hits = await search_chunks(session, embeddings, query, tenant_id=tenant_id, limit=limit)
    finally:
        await engine.dispose()

    if not hits:
        print("No chunks found: run `make ingest` with this embedding model first.")
    for rank, hit in enumerate(hits, start=1):
        body = hit.content.split("\n\n", 1)[-1]  # drop the heading path, printed above
        print(
            f"{rank}. similarity {hit.similarity:.3f} | {hit.doc_key} v{hit.version} "
            f"p.{hit.page_number}\n   {' > '.join(hit.section_path)}"
        )
        print(textwrap.indent(textwrap.shorten(body, SNIPPET_CHARS), "   "), end="\n\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("query")
    parser.add_argument("--tenant", default="nsi-amer")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(args.query, args.tenant, args.limit), loop_factory=loop_factory)
