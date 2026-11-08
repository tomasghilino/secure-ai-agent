import math
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.rag.embeddings import (
    QWEN3_QUERY_INSTRUCTION,
    EmbeddingError,
    FakeEmbeddings,
    OllamaEmbeddings,
    build_embedding_provider,
    cosine_similarity,
)


def test_cosine_similarity_basic_geometry() -> None:
    assert cosine_similarity([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)  # length ignored
    assert cosine_similarity([1.0, 0.0], [0.0, 3.0]) == pytest.approx(0.0)  # orthogonal
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)  # opposite
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0  # zero vector is "unrelated"


def test_cosine_similarity_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="dimension mismatch"):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


async def test_fake_embeddings_are_deterministic_normalized_and_sized() -> None:
    provider = FakeEmbeddings(dimensions=256)

    first, second = await provider.embed_documents(["Returns policy", "Returns policy"])

    assert first == second
    assert len(first) == 256
    assert math.sqrt(sum(x * x for x in first)) == pytest.approx(1.0)


async def test_fake_embeddings_reward_word_overlap_only() -> None:
    provider = FakeEmbeddings()
    query = await provider.embed_query("refund policy for returned pumps")
    overlap, unrelated = await provider.embed_documents(
        ["Refund policy: returned pumps are inspected", "Travel expenses need receipts"]
    )

    assert cosine_similarity(query, overlap) > cosine_similarity(query, unrelated)


class _RecordingOllamaClient:
    """Test double for ollama.AsyncClient: records inputs, returns fixed-size vectors."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed(self, *, model: str, input: Sequence[str], dimensions: int) -> Any:
        self.calls.append(list(input))
        return SimpleNamespace(embeddings=[[3.0] + [0.0] * (self.dimensions - 1) for _ in input])


async def test_ollama_embeddings_batches_and_normalizes() -> None:
    client = _RecordingOllamaClient(dimensions=8)
    provider = OllamaEmbeddings(
        model="qwen3-embedding:0.6b",
        dimensions=8,
        batch_size=2,
        client=client,  # type: ignore[arg-type]
    )

    vectors = await provider.embed_documents(["a", "b", "c"])

    assert client.calls == [["a", "b"], ["c"]]
    assert vectors[0][0] == pytest.approx(1.0)  # 3.0 normalized to unit length


async def test_ollama_query_instruction_applies_to_queries_only() -> None:
    client = _RecordingOllamaClient(dimensions=4)
    provider = OllamaEmbeddings(
        model="qwen3-embedding:0.6b",
        dimensions=4,
        query_instruction=QWEN3_QUERY_INSTRUCTION,
        client=client,  # type: ignore[arg-type]
    )

    await provider.embed_documents(["Return window is 30 days."])
    await provider.embed_query("How long is the return window?")

    assert client.calls[0] == ["Return window is 30 days."]
    assert client.calls[1][0].startswith(f"Instruct: {QWEN3_QUERY_INSTRUCTION}\nQuery:")


async def test_ollama_embeddings_reject_wrong_dimensions() -> None:
    provider = OllamaEmbeddings(
        model="some-model",
        dimensions=1024,
        client=_RecordingOllamaClient(dimensions=768),  # type: ignore[arg-type]
    )

    with pytest.raises(EmbeddingError, match="1024-d"):
        await provider.embed_documents(["text"])


def test_factory_builds_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")

    provider = build_embedding_provider(Settings(_env_file=None))  # type: ignore[call-arg]

    assert provider.model_id == "ollama/qwen3-embedding:0.6b"
    assert provider.dimensions == 1024
