"""Shared test configuration.

Tests must never depend on a developer's local `.env` (which may point at Ollama or
Bedrock): environment variables override `.env`, so pin the free, deterministic defaults.
"""

import asyncio
from collections.abc import Iterator, Mapping

import pytest

from app.core.config import get_settings
from scripts.generate_pdfs import SOURCE_DIR, generate


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, type[asyncio.AbstractEventLoop]]:
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    # The selector loop is already the default on Linux and macOS.
    return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def returns_policy_pdf(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Active Returns Policy v3.0 (3 pages), generated from the canonical YAML sources."""
    output = tmp_path_factory.mktemp("generated")
    manifest = generate(SOURCE_DIR, output)
    entry = next(d for d in manifest if d["doc_id"] == "NSI-POL-RET-003" and d["version"] == "3.0")
    return (output / entry["file"]).read_bytes()
