"""Shared test configuration.

Tests must never depend on a developer's local `.env` (which may point at Ollama or
Bedrock): environment variables override `.env`, so pin the free, deterministic defaults.
"""

import asyncio
from collections.abc import Iterator, Mapping

import pytest

from app.core.config import get_settings
from app.ingestion.pipeline import DocumentMetadata
from scripts.generate_pdfs import SOURCE_DIR, generate
from scripts.ingest_samples import metadata_from_manifest

# (doc_key, version) -> (metadata, PDF bytes)
GeneratedPdfs = dict[tuple[str, str], tuple[DocumentMetadata, bytes]]


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
def generated_pdfs(
    tmp_path_factory: pytest.TempPathFactory,
) -> GeneratedPdfs:
    """Every synthetic PDF with its metadata, keyed by (doc_key, version).

    Generated once per run from the canonical YAML sources.
    """
    output = tmp_path_factory.mktemp("generated")
    return {
        (entry["doc_id"], entry["version"]): (
            metadata_from_manifest(entry),
            (output / entry["file"]).read_bytes(),
        )
        for entry in generate(SOURCE_DIR, output)
    }


@pytest.fixture(scope="session")
def returns_policy_pdf(generated_pdfs: GeneratedPdfs) -> bytes:
    """Active Returns Policy v3.0 (3 pages)."""
    return generated_pdfs["NSI-POL-RET-003", "3.0"][1]
