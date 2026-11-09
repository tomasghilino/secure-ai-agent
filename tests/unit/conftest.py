"""Fixtures shared by the ingestion unit tests."""

import pytest

from scripts.generate_pdfs import SOURCE_DIR, generate


@pytest.fixture(scope="session")
def returns_policy_pdf(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """Active Returns Policy v3.0 (3 pages), generated from the canonical YAML sources."""
    output = tmp_path_factory.mktemp("generated")
    manifest = generate(SOURCE_DIR, output)
    entry = next(d for d in manifest if d["doc_id"] == "NSI-POL-RET-003" and d["version"] == "3.0")
    return (output / entry["file"]).read_bytes()
