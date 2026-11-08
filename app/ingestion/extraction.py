"""PDF validation and text extraction (ingestion step 2).

A PDF does not store paragraphs: it stores glyphs drawn at x/y positions. Extraction
rebuilds the reading order. Two decisions matter for RAG:

1. **One record per page.** Citations must point to the original page number, so text is
   never merged across pages at this stage.
2. **Markdown, not plain text.** ``pymupdf4llm`` keeps headings (``## 3. Return
   Eligibility``) and table rows (``|NX-4400B|...|36|``). Plain text flattens a table into
   loose lines and loses which value belongs to which product.

Extraction is CPU-bound and synchronous; async callers run it in a worker thread.
Uploaded files are untrusted: validate before parsing and cap the page count.
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf
import pymupdf4llm

PDF_MAGIC = b"%PDF-"


class PdfValidationError(ValueError):
    """The file is not a PDF we are willing to process."""


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_number: int  # 1-based, as printed on the page
    text: str  # Markdown


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    pages: tuple[ExtractedPage, ...]
    pdf_metadata: dict[str, str]  # PDF info dictionary (title, author, keywords, ...)
    embedded_fields: dict[str, str]  # `key=value;...` pairs found in the keywords field

    @property
    def page_count(self) -> int:
        return len(self.pages)


def parse_embedded_fields(keywords: str) -> dict[str, str]:
    """Parse ``doc_id=NSI-POL-RET-003;version=3.0`` into a dict, ignoring malformed parts."""
    fields: dict[str, str] = {}
    for part in keywords.split(";"):
        key, sep, value = part.partition("=")
        if sep and key.strip() and value.strip():
            fields[key.strip()] = value.strip()
    return fields


def extract_pdf(data: bytes, *, max_pages: int = 200) -> ExtractedDocument:
    if not data.startswith(PDF_MAGIC):
        raise PdfValidationError("not a PDF file (missing %PDF- header)")
    try:
        document = pymupdf.open(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise PdfValidationError("corrupted or unreadable PDF") from exc

    with document:
        if document.needs_pass:
            raise PdfValidationError("encrypted PDFs are not supported")
        if not 1 <= document.page_count <= max_pages:
            raise PdfValidationError(f"PDF must have between 1 and {max_pages} pages")

        page_chunks = pymupdf4llm.to_markdown(
            document, page_chunks=True, use_ocr=False, show_progress=False
        )
        pages = tuple(
            ExtractedPage(page_number=index + 1, text=chunk["text"])
            for index, chunk in enumerate(page_chunks)
        )
        metadata = {k: v for k, v in (document.metadata or {}).items() if isinstance(v, str) and v}

    return ExtractedDocument(
        pages=pages,
        pdf_metadata=metadata,
        embedded_fields=parse_embedded_fields(metadata.get("keywords", "")),
    )
