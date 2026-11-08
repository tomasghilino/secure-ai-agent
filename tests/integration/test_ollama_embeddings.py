"""Real-model checks against the local Ollama server (free, no AWS). Skipped if unreachable."""

import pytest

from app.core.config import Settings
from app.rag.embeddings import EmbeddingProvider, build_embedding_provider, cosine_similarity

pytestmark = pytest.mark.llm


@pytest.fixture
async def provider() -> EmbeddingProvider:
    embeddings = build_embedding_provider(
        Settings(_env_file=None, embedding_provider="ollama")  # type: ignore[call-arg]
    )
    try:
        await embeddings.embed_query("ping")
    except Exception as exc:  # connection refused, model not pulled, ...
        pytest.skip(f"Ollama not available: {exc}")
    return embeddings


async def test_returns_vectors_of_configured_size(provider: EmbeddingProvider) -> None:
    vectors = await provider.embed_documents(["Return window", "Warranty terms"])

    assert [len(v) for v in vectors] == [1024, 1024]


async def test_recognises_paraphrases_without_shared_words(provider: EmbeddingProvider) -> None:
    query = await provider.embed_query("How many days does a client have to send back a product?")
    returns, warranty = await provider.embed_documents(
        [
            "Customers may return eligible equipment within 30 calendar days of delivery.",
            "The NX-4400 hydraulic pump carries a 24-month limited warranty.",
        ]
    )

    assert cosine_similarity(query, returns) > cosine_similarity(query, warranty)
