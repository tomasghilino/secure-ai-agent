"""Shared test configuration.

Tests must never depend on a developer's local `.env` (which may point at Ollama or
Bedrock): environment variables override `.env`, so pin the free, deterministic defaults.
"""

from collections.abc import Iterator

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
