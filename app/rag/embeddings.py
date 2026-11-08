"""Embedding providers.

An embedding model maps a text to a fixed-length vector of floats such that texts with
similar *meaning* end up close together (high cosine similarity). Retrieval quality is
bounded by the embedding model, so the provider is an explicit, swappable dependency:

- ``FakeEmbeddings``   deterministic feature hashing (bag of words). Free and reproducible,
  it only captures shared words: good for tests, meaningless for quality benchmarks.
- ``OllamaEmbeddings`` local open-weight model (Qwen3-Embedding) on the developer's GPU.
- Bedrock Titan V2 is added together with the AWS adapters (same interface).

Documents and queries go through different methods because some models are
*asymmetric*: Qwen3-Embedding expects a task instruction in front of queries only.

All providers return L2-normalized vectors, so cosine similarity equals the dot product.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

from ollama import AsyncClient

from app.core.config import Settings

Vector = list[float]

# Retrieval instruction recommended by the Qwen3-Embedding authors for query-side inputs.
QWEN3_QUERY_INSTRUCTION = (
    "Given an employee question, retrieve passages from company documents that answer it"
)


class EmbeddingProvider(Protocol):
    """What the rest of the system needs from any embedding model."""

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]: ...

    async def embed_query(self, text: str) -> Vector: ...


class EmbeddingError(RuntimeError):
    """The provider returned something unusable (wrong size, wrong count)."""


def l2_normalize(vector: Sequence[float]) -> Vector:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return [0.0] * len(vector)
    return [x / norm for x in vector]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """cos(θ) = (a · b) / (‖a‖ ‖b‖). 1 = same direction, 0 = unrelated."""
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class FakeEmbeddings:
    """Feature hashing: every token adds ±1 to one hashed dimension, then L2-normalize.

    Texts that share words share dimensions, so word overlap produces similarity.
    Paraphrases with different words ("send back" vs "return") are NOT similar here —
    which is exactly the gap a real embedding model closes.
    """

    def __init__(self, dimensions: int = 1024) -> None:
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"fake-feature-hashing-{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _embed(self, text: str) -> Vector:
        vector = [0.0] * self._dimensions
        for token in _TOKEN.findall(text.lower()):
            value = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest())
            index = value % self._dimensions
            sign = 1.0 if value >> 63 else -1.0
            vector[index] += sign
        return l2_normalize(vector)

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> Vector:
        return self._embed(text)


class OllamaEmbeddings:
    """Embeddings from a local Ollama server (``POST /api/embed``), batched."""

    def __init__(
        self,
        *,
        model: str,
        dimensions: int,
        base_url: str = "http://localhost:11434",
        query_instruction: str | None = None,
        batch_size: int = 32,
        client: AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._query_instruction = query_instruction
        self._batch_size = batch_size
        self._client = client or AsyncClient(host=base_url)

    @property
    def model_id(self) -> str:
        return f"ollama/{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _embed_batch(self, inputs: list[str]) -> list[Vector]:
        response = await self._client.embed(
            model=self._model, input=inputs, dimensions=self._dimensions
        )
        vectors = [l2_normalize(v) for v in response.embeddings]
        if len(vectors) != len(inputs):
            raise EmbeddingError(f"expected {len(inputs)} vectors, got {len(vectors)}")
        if any(len(v) != self._dimensions for v in vectors):
            raise EmbeddingError(f"{self._model} did not return {self._dimensions}-d vectors")
        return vectors

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        vectors: list[Vector] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(await self._embed_batch(list(texts[start : start + self._batch_size])))
        return vectors

    async def embed_query(self, text: str) -> Vector:
        if self._query_instruction:
            text = f"Instruct: {self._query_instruction}\nQuery:{text}"
        return (await self._embed_batch([text]))[0]


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Composition root for embeddings: the only place that knows concrete classes."""
    match settings.embedding_provider:
        case "fake":
            return FakeEmbeddings(settings.embedding_dimensions)
        case "ollama":
            model = settings.ollama_embedding_model
            return OllamaEmbeddings(
                model=model,
                dimensions=settings.embedding_dimensions,
                base_url=settings.ollama_base_url,
                query_instruction=QWEN3_QUERY_INSTRUCTION if "qwen3-embedding" in model else None,
            )
        case "bedrock":
            raise NotImplementedError("Bedrock Titan V2 embeddings arrive with the AWS adapters")
