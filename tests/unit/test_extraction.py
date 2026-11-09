import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

from app.ingestion.extraction import PdfValidationError, extract_pdf, parse_embedded_fields


def test_parse_embedded_fields_ignores_malformed_parts() -> None:
    fields = parse_embedded_fields("doc_id=NSI-1; version=3.0;broken;=x;empty=")

    assert fields == {"doc_id": "NSI-1", "version": "3.0"}


def test_extracts_one_record_per_page_with_original_numbers(returns_policy_pdf: bytes) -> None:
    document = extract_pdf(returns_policy_pdf)

    assert document.page_count == 3
    assert [p.page_number for p in document.pages] == [1, 2, 3]
    assert "30 calendar days of the delivery date" in document.pages[0].text


def test_reads_metadata_embedded_by_the_generator(returns_policy_pdf: bytes) -> None:
    document = extract_pdf(returns_policy_pdf)

    assert document.embedded_fields["doc_id"] == "NSI-POL-RET-003"
    assert document.embedded_fields["version"] == "3.0"
    assert document.embedded_fields["effective_date"] == "2026-03-01"


def test_tables_keep_row_structure(returns_policy_pdf: bytes) -> None:
    text = "\n".join(p.text for p in extract_pdf(returns_policy_pdf).pages)

    # Markdown keeps the cells of one row together, so the fee stays tied to its reason.
    assert any("Non-defective return" in line and "15%" in line for line in text.splitlines())


def test_rejects_files_that_are_not_pdfs() -> None:
    with pytest.raises(PdfValidationError, match="not a PDF"):
        extract_pdf(b"MZ\x90\x00 definitely an executable")


def test_rejects_corrupted_pdfs() -> None:
    with pytest.raises(PdfValidationError, match="corrupted"):
        extract_pdf(b"%PDF-1.7\n garbage without any objects")


def test_rejects_encrypted_pdfs() -> None:
    document = pymupdf.open()
    document.new_page()
    encrypted = document.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner-secret", user_pw="user-secret"
    )

    with pytest.raises(PdfValidationError, match="encrypted"):
        extract_pdf(encrypted)


def test_enforces_page_limit(returns_policy_pdf: bytes) -> None:
    with pytest.raises(PdfValidationError, match="between 1 and 2 pages"):
        extract_pdf(returns_policy_pdf, max_pages=2)


def test_inspect_script_prints_metadata_and_page(returns_policy_pdf: bytes, tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(returns_policy_pdf)

    result = subprocess.run(  # noqa: S603  # fixed interpreter + module, test-controlled args
        [sys.executable, "-m", "scripts.inspect_pdf", str(pdf), "--page", "2"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    assert "doc_id=NSI-POL-RET-003" in result.stdout
    assert "page 2" in result.stdout
