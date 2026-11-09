"""Text normalization (ingestion step 3).

Extraction returns one Markdown string per page. Before chunking we remove noise that
hurts retrieval and keep everything that carries meaning.

* **Running headers and footers** (document title, "Page 2 of 3") repeat on every page.
  Left in, they end up in every chunk: unrelated chunks get similar embeddings, keyword
  search matches every page, and they waste context tokens. Nothing is lost: title,
  version and page number already live in metadata, which is what citations use.
* **Extraction artifacts**: ``<br>`` inside table cells, ``**bold**`` markers, irregular
  spacing and invisible Unicode characters.

Kept on purpose: Markdown headings (``## 3. Return Eligibility``) and table rows, because
the chunker uses them. Pages are never merged, so page numbers stay exact.

Known limitations (fine for our PDFs, worth revisiting for real-world files): no
de-hyphenation of words split across lines, no OCR cleanup, and headers are detected by
repetition, so a one-page document only loses its page-number line.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.ingestion.extraction import ExtractedPage

# Soft hyphen, zero-width characters, word joiner and byte-order mark.
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"))
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BOLD = re.compile(r"\*\*")
_INNER_SPACES = re.compile(r"(?<=\S)[ \t]+")  # leading indentation (nested lists) is kept
_SPACE_BEFORE_PUNCT = re.compile(r"(?<=\w) +(?=[.,;:!?](?:\s|$))")  # "30 days ." -> "30 days."
_BLANK_LINES = re.compile(r"\n{3,}")
_PAGE_NUMBER = re.compile(r"(?:page\s+)?\d+(?:\s*(?:of|/)\s*\d+)?", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    pages: tuple[ExtractedPage, ...]
    removed_boilerplate: tuple[str, ...]  # one example per removed header/footer line


def clean_text(text: str) -> str:
    """Page-independent cleanup. It changes formatting, never removes words."""
    text = unicodedata.normalize("NFKC", text).translate(
        _INVISIBLE
    )  # NBSP -> space, ligatures -> letters
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _BR.sub(" ", text)
    text = _BOLD.sub("", text)
    lines = (
        _SPACE_BEFORE_PUNCT.sub("", _INNER_SPACES.sub(" ", line.rstrip()))
        for line in text.split("\n")
    )
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip("\n")


def _boilerplate_key(line: str) -> str:
    """Compare lines ignoring case, spacing and numbers, so "Page 1 of 3" == "Page 2 of 3"."""
    return _DIGITS.sub("#", " ".join(line.lower().split()))


def _is_candidate(line: str) -> bool:
    # Table rows are never boilerplate: a table that spans pages repeats its header row,
    # and the continuation page needs it to make sense.
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("|")


def _edge_indexes(lines: list[str], edge_lines: int) -> set[int]:
    """Indexes of the first and last ``edge_lines`` non-empty lines (where headers live)."""
    content = [i for i, line in enumerate(lines) if line.strip()]
    return set(content[:edge_lines] + content[-edge_lines:])


def normalize_pages(
    pages: Sequence[ExtractedPage], *, edge_lines: int = 3, min_page_ratio: float = 0.6
) -> NormalizedDocument:
    """Clean every page and drop running headers/footers.

    A line is a header/footer when it sits near the top or bottom of a page and repeats
    (ignoring numbers) on at least ``min_page_ratio`` of the pages, minimum two pages.
    Page-number lines ("Page 3 of 7", "3/7", "3") are dropped even on single-page files.
    """
    page_lines = [clean_text(page.text).split("\n") for page in pages]
    edges = [_edge_indexes(lines, edge_lines) for lines in page_lines]

    counts: Counter[str] = Counter()
    for lines, indexes in zip(page_lines, edges, strict=True):
        counts.update({_boilerplate_key(lines[i]) for i in indexes if _is_candidate(lines[i])})
    min_pages = max(2, math.ceil(min_page_ratio * len(pages)))
    repeated = {key for key, seen_on in counts.items() if seen_on >= min_pages}

    removed: dict[str, str] = {}
    normalized: list[ExtractedPage] = []
    for page, lines, indexes in zip(pages, page_lines, edges, strict=True):
        drop: set[int] = set()
        for i in indexes:
            line = lines[i].strip()
            if not _is_candidate(line):
                continue
            key = _boilerplate_key(line)
            if key in repeated or _PAGE_NUMBER.fullmatch(line):
                drop.add(i)
                removed.setdefault(key, line)
        text = "\n".join(line for i, line in enumerate(lines) if i not in drop)
        normalized.append(
            ExtractedPage(
                page_number=page.page_number, text=_BLANK_LINES.sub("\n\n", text).strip("\n")
            )
        )

    return NormalizedDocument(pages=tuple(normalized), removed_boilerplate=tuple(removed.values()))
