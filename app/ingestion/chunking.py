"""Page-aware chunking (ingestion step 4).

A chunk is what retrieval returns and what the LLM reads to answer, so every chunk must
make sense on its own and point to the page it came from.

1. **Split at headings.** Markdown headings (``## 3. Return Eligibility``) mark topic
   boundaries, so each section becomes its own chunk. Cutting every N characters would
   separate a table row from its header row, or a heading from its text.
2. **Never cross a page.** A section that continues on the next page becomes two chunks,
   one per page, so every citation points to one exact page.
3. **Carry the context.** Each chunk keeps its heading path
   (``Returns and Refunds Policy > 3. Return Eligibility``) and the path is prepended to the
   text that gets embedded. A continuation chunk on page 2 still says what it is about.
4. **Cap the size.** A section longer than ``max_chars`` is split at the gentlest boundary
   available: between paragraphs/tables first, then between table rows (every part repeats
   the header row), then between sentences. Rows, sentences and words are never cut.

Heading lines are not repeated in the body: they live in the path. There is no overlap
between chunks: each one already carries its heading path, and overlap would repeat text
in the index and in the LLM context.

The limit is in characters, not tokens: every model has its own tokenizer, and English
averages about 4 characters per token, so 1,500 characters is roughly 375 tokens.

Known limitation: a table that continues on the next page is only self-explanatory when
the PDF repeats its header row there (ours do; many real PDFs do not).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from app.ingestion.extraction import ExtractedPage

DEFAULT_MAX_CHARS = 1500
PATH_SEPARATOR = " > "
_HEADING = re.compile(r"^(#{1,6}) +(.+?)\s*$")
_BLANK_LINE = re.compile(r"\n\s*\n")
_TABLE_SEPARATOR = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+$")  # the |---|---| line under a header
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int  # 0-based position in the document
    page_number: int  # a chunk never crosses pages, so citations are exact
    section_path: tuple[str, ...]  # ("Returns and Refunds Policy", "3. Return Eligibility")
    body: str  # the section's text on this page, without heading lines

    @property
    def text(self) -> str:
        """What gets embedded and indexed: the heading path, then the body."""
        if not self.section_path:
            return self.body
        return f"{PATH_SEPARATOR.join(self.section_path)}\n\n{self.body}"


def _page_sections(pages: Sequence[ExtractedPage]) -> Iterator[tuple[int, tuple[str, ...], str]]:
    """Yield ``(page_number, heading_path, body)`` for each piece of a section on a page.

    The heading stack survives page breaks: text at the top of page 2 belongs to the last
    heading seen on page 1. Bodies can be empty (a heading followed by another heading).
    """
    headings: list[tuple[int, str]] = []  # open headings as (level, title), outermost first
    for page in pages:
        lines: list[str] = []
        for line in page.text.split("\n"):
            heading = _HEADING.match(line)
            if heading is None:
                lines.append(line)
                continue
            yield page.page_number, tuple(title for _, title in headings), "\n".join(lines).strip()
            level = len(heading.group(1))
            while headings and headings[-1][0] >= level:  # a "##" closes the previous "##"
                headings.pop()
            headings.append((level, heading.group(2)))
            lines = []
        yield page.page_number, tuple(title for _, title in headings), "\n".join(lines).strip()


def _pack(pieces: Iterable[str], max_chars: int, separator: str) -> list[str]:
    """Join consecutive pieces while the result fits in ``max_chars``.

    A single piece that is already too long (a huge row or sentence) is kept whole.
    """
    packed: list[str] = []
    for piece in pieces:
        if packed and len(packed[-1]) + len(separator) + len(piece) <= max_chars:
            packed[-1] += separator + piece
        else:
            packed.append(piece)
    return packed


def _split_table(table: str, max_chars: int) -> list[str]:
    """Split a Markdown table between rows; every part repeats the header row."""
    lines = table.split("\n")
    has_header = len(lines) > 2 and _TABLE_SEPARATOR.match(lines[1]) is not None
    if not has_header:
        return _pack(lines, max_chars, "\n")
    header = "\n".join(lines[:2])
    groups = _pack(lines[2:], max_chars - len(header) - 1, "\n")
    return [f"{header}\n{group}" for group in groups]


def _split_block(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    if block.startswith("|"):
        return _split_table(block, max_chars)
    return _pack(_SENTENCE_END.split(block), max_chars, " ")


def _split_body(body: str, max_chars: int) -> list[str]:
    """Split a section body into parts of at most ``max_chars``, gentlest boundary first."""
    if len(body) <= max_chars:
        return [body]
    parts = (
        part
        for block in _BLANK_LINE.split(body)
        if block.strip()
        for part in _split_block(block.strip(), max_chars)
    )
    return _pack(parts, max_chars, "\n\n")


def chunk_pages(
    pages: Sequence[ExtractedPage], *, max_chars: int = DEFAULT_MAX_CHARS
) -> list[Chunk]:
    """Split normalized pages into chunks: one per section per page, split if too long."""
    pieces = [
        (page, path, part)
        for page, path, body in _page_sections(pages)
        if body
        for part in _split_body(body, max_chars)
    ]
    return [
        Chunk(index=index, page_number=page, section_path=path, body=body)
        for index, (page, path, body) in enumerate(pieces)
    ]
