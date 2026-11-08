"""Compare how two embedding providers rank the same passages for one question.

Shows why semantic embeddings matter: the fake (bag-of-words) provider rewards shared
words, while a real model recognises paraphrases ("send back" ~ "return").

Usage (from the repository root, with the `llm` compose profile running):
    uv run python -m scripts.embedding_playground                  # fake vs ollama
    uv run python -m scripts.embedding_playground --query "..."    # your own question
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.config import Settings
from app.rag.embeddings import EmbeddingProvider, build_embedding_provider, cosine_similarity

PASSAGES = [
    "Customers may return eligible equipment within 30 calendar days of delivery "
    "for a full refund.",
    "Employees must submit travel expense reports within 30 days after the trip ends.",
    "The NX-4400 hydraulic pump carries a 24-month limited warranty.",
    "Refund requests require the original order number and an RMA authorization.",
]
DEFAULT_QUERY = "How many days does a client have to send back a product?"


async def rank(provider: EmbeddingProvider, query: str) -> None:
    query_vector = await provider.embed_query(query)
    passage_vectors = await provider.embed_documents(PASSAGES)
    pairs = zip(passage_vectors, PASSAGES, strict=True)
    scored = sorted(((cosine_similarity(query_vector, v), p) for v, p in pairs), reverse=True)
    print(f"\n{provider.model_id}  (dims={provider.dimensions})")
    print(f"  query vector, first 5 values: {[round(x, 4) for x in query_vector[:5]]}")
    for score, passage in scored:
        print(f"  {score:+.4f}  {passage[:78]}")


async def main(query: str) -> None:
    print(f"Query: {query}")
    for provider_name in ("fake", "ollama"):
        settings = Settings(embedding_provider=provider_name)
        await rank(build_embedding_provider(settings), query)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", default=DEFAULT_QUERY)
    asyncio.run(main(parser.parse_args().query))
