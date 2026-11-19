"""Context assembly: which retrieved chunks go into the prompt, and how they are framed.

- **Token budget.** Chunks are added in ranking order while they fit in ``max_tokens``.
  More context is not better: it costs more, adds latency, and buries the relevant
  passage among weaker ones. Tokens are estimated (about 4 characters per token in
  English), which is enough for a budget; the provider reports the exact count.
- **Numbered, delimited sources.** Each chunk becomes ``<source id="N" ...>`` with its
  document, version, page and section. The model cites ``[N]``; the code maps N back to the
  chunk, so citations always point to real pages.
- **Untrusted data.** A document could contain text like "ignore your instructions". The
  sources are wrapped in tags the system prompt declares as data, and any tag-like text
  inside a chunk is neutralised so it cannot close the wrapper early.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.rag.retrieval import SearchHit

CHARS_PER_TOKEN = 4
_SOURCE_TAG = re.compile(r"</?\s*source", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Source:
    number: int  # what the model cites: [number]
    hit: SearchHit


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def select_sources(hits: Sequence[SearchHit], *, max_tokens: int) -> list[Source]:
    """The best-ranked chunks that fit in the budget, numbered from 1."""
    selected: list[Source] = []
    used = 0
    for hit in hits:
        cost = estimate_tokens(hit.content)
        if used + cost > max_tokens:
            continue  # too big for what is left; a smaller, lower-ranked chunk may still fit
        selected.append(Source(len(selected) + 1, hit))
        used += cost
    return selected


def render_sources(sources: Sequence[Source]) -> str:
    blocks = []
    for source in sources:
        hit = source.hit
        attributes = {
            "id": str(source.number),
            "document": hit.doc_key,
            "version": hit.version,
            "title": hit.title,
            "page": str(hit.page_number),
            "section": hit.section_path[-1],
        }
        rendered = " ".join(f'{key}="{html.escape(value)}"' for key, value in attributes.items())
        content = _SOURCE_TAG.sub("&lt;source", hit.content)
        blocks.append(f"<source {rendered}>\n{content}\n</source>")
    return "\n\n".join(blocks)
