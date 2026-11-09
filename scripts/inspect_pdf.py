"""Show what the ingestion pipeline "sees" when it reads a PDF.

Usage (from the repository root):
    uv run python -m scripts.inspect_pdf data/generated/pdfs/nsi-amer/NSI-POL-RET-003_v3.0.pdf
    uv run python -m scripts.inspect_pdf <file.pdf> --page 2
    uv run python -m scripts.inspect_pdf <file.pdf> --page 2 --normalized
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from app.ingestion.extraction import extract_pdf
from app.ingestion.normalization import normalize_pages


def main() -> None:
    parser = argparse.ArgumentParser(description="Print extracted metadata and page text.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", type=int, default=1, help="1-based page number to print")
    parser.add_argument(
        "--normalized", action="store_true", help="show the text after normalization (step 3)"
    )
    args = parser.parse_args()

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    document = extract_pdf(args.pdf.read_bytes())

    print(f"File:            {args.pdf}")
    print(f"Pages:           {document.page_count}")
    print(f"PDF title:       {document.pdf_metadata.get('title', '-')}")
    print("Embedded fields: " + ", ".join(f"{k}={v}" for k, v in document.embedded_fields.items()))
    pages = document.pages
    if args.normalized:
        normalized = normalize_pages(document.pages)
        pages = normalized.pages
        print("Removed lines:   " + " / ".join(normalized.removed_boilerplate))
    page = pages[args.page - 1]
    print(f"\n========== page {page.page_number} ({len(page.text)} characters) ==========\n")
    print(page.text)


if __name__ == "__main__":
    main()
