"""Ingest the generated sample PDFs into PostgreSQL with the configured embedding model.

Needs `make up`, `make migrate` and `make data`; with EMBEDDING_PROVIDER=ollama also
`make llm-up`. Usage (from the repository root):
    uv run python -m scripts.ingest_samples

Run it twice: the second run skips every file (idempotent ingestion).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.database.session import build_engine, build_session_factory
from app.ingestion.pipeline import DocumentMetadata, ingest_pdf
from app.rag.embeddings import build_embedding_provider

GENERATED_DIR = Path(__file__).resolve().parents[1] / "data" / "generated"


def metadata_from_manifest(entry: dict[str, Any]) -> DocumentMetadata:
    return DocumentMetadata(
        tenant_id=entry["tenant_id"],
        doc_key=entry["doc_id"],
        version=entry["version"],
        title=entry["title"],
        status=entry["status"],
        effective_date=date.fromisoformat(entry["effective_date"]),
        classification=entry["classification"],
    )


async def main() -> None:
    settings = get_settings()
    embeddings = build_embedding_provider(settings)
    manifest: list[dict[str, Any]] = json.loads((GENERATED_DIR / "manifest.json").read_text())
    print(f"Embedding model: {embeddings.model_id}\n")

    engine = build_engine(settings.database_url)
    try:
        async with build_session_factory(engine)() as session:
            for entry in manifest:
                data = (GENERATED_DIR / entry["file"]).read_bytes()
                result = await ingest_pdf(session, data, metadata_from_manifest(entry), embeddings)
                outcome = (
                    "skipped (already ingested)"
                    if result.skipped
                    else (f"stored {result.chunk_count} chunks")
                )
                print(f"{entry['doc_id']:<20} v{entry['version']:<8} {outcome}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
