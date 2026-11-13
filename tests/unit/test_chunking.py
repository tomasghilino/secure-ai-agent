from app.ingestion.chunking import Chunk, chunk_pages
from app.ingestion.extraction import ExtractedPage, extract_pdf
from app.ingestion.normalization import normalize_pages


def _pages(*texts: str) -> list[ExtractedPage]:
    return [ExtractedPage(page_number=i + 1, text=text) for i, text in enumerate(texts)]


def test_each_section_becomes_a_chunk_with_its_heading_path() -> None:
    page = (
        "# Travel Policy\n\nAcme Corp - Finance\n\n"
        "## 1. Meals\n\nDaily limit is 60 USD.\n\n"
        "## 2. Hotels\n\n|City|Limit|\n|---|---|\n|Paris|220|"
    )

    chunks = chunk_pages(_pages(page))

    assert [(c.section_path, c.body) for c in chunks] == [
        (("Travel Policy",), "Acme Corp - Finance"),
        (("Travel Policy", "1. Meals"), "Daily limit is 60 USD."),
        (("Travel Policy", "2. Hotels"), "|City|Limit|\n|---|---|\n|Paris|220|"),
    ]
    assert [c.index for c in chunks] == [0, 1, 2]


def test_a_section_that_continues_on_the_next_page_becomes_one_chunk_per_page() -> None:
    pages = _pages(
        "# SLA\n\n## 4. Targets\n\n|Priority|Hours|\n|---|---|\n|P1|4|",
        "|Priority|Hours|\n|---|---|\n|P3|48|\n\n## 5. Channels\n\nUse the portal.",
    )

    chunks = chunk_pages(pages)

    assert [(c.page_number, c.section_path) for c in chunks] == [
        (1, ("SLA", "4. Targets")),
        (2, ("SLA", "4. Targets")),  # the continuation keeps the heading from page 1
        (2, ("SLA", "5. Channels")),
    ]
    assert chunks[1].body == "|Priority|Hours|\n|---|---|\n|P3|48|"


def test_nested_headings_open_and_close_like_a_table_of_contents() -> None:
    page = "# Policy\n\n## 1. Scope\n\n### 1.1 Staff\n\nAll staff.\n\n## 2. Rules\n\nBe safe."

    chunks = chunk_pages(_pages(page))

    assert [c.section_path for c in chunks] == [
        ("Policy", "1. Scope", "1.1 Staff"),
        ("Policy", "2. Rules"),  # "## 2." closes both "## 1." and "### 1.1"
    ]


def test_text_prepends_the_heading_path_to_the_body() -> None:
    chunk = Chunk(index=0, page_number=2, section_path=("SLA", "4. Targets"), body="|P3|48|")

    assert chunk.text == "SLA > 4. Targets\n\n|P3|48|"
    assert Chunk(index=0, page_number=1, section_path=(), body="Intro.").text == "Intro."


def test_chunks_the_generated_returns_policy(returns_policy_pdf: bytes) -> None:
    pages = normalize_pages(extract_pdf(returns_policy_pdf).pages).pages

    chunks = chunk_pages(pages)

    assert len(chunks) == 10  # the title block + 9 numbered sections
    assert chunks[0].section_path == ("Returns and Refunds Policy",)
    assert "|Document ID|NSI-POL-RET-003|Version|3.0|" in chunks[0].body
    eligibility = next(c for c in chunks if c.section_path[-1] == "3. Return Eligibility")
    assert eligibility.page_number == 1
    assert eligibility.text.startswith("Returns and Refunds Policy > 3. Return Eligibility\n\n")
    assert "within 30 calendar days of the delivery date" in eligibility.body
    assert [c.page_number for c in chunks] == [1, 1, 1, 1, 2, 2, 2, 2, 3, 3]


def test_a_long_section_is_split_between_paragraphs_and_keeps_its_context() -> None:
    paragraphs = [(f"Paragraph {n} " + "lorem " * 15).strip() for n in range(1, 5)]  # 101 chars
    page = "# Policy\n\n## 1. Rules\n\n" + "\n\n".join(paragraphs)

    chunks = chunk_pages(_pages(page), max_chars=250)

    assert [c.body for c in chunks] == ["\n\n".join(paragraphs[:2]), "\n\n".join(paragraphs[2:])]
    assert all(c.section_path == ("Policy", "1. Rules") and c.page_number == 1 for c in chunks)


def test_a_long_table_is_split_between_rows_and_repeats_its_header() -> None:
    header = "|SKU|Price|\n|---|---|"
    rows = [f"|NX-{n:04d}|{n * 100}|" for n in range(1, 9)]
    page = "# Catalog\n\n## Pumps\n\n" + "\n".join([header, *rows])

    chunks = chunk_pages(_pages(page), max_chars=60)

    assert len(chunks) == 4
    assert all(c.body.startswith(header + "\n") and len(c.body) <= 60 for c in chunks)
    assert [row for c in chunks for row in c.body.split("\n")[2:]] == rows  # none lost or cut


def test_a_long_paragraph_is_split_between_sentences() -> None:
    sentences = [f"Sentence number {n} ends here." for n in range(1, 7)]  # 28 chars each

    chunks = chunk_pages(_pages("## Notes\n\n" + " ".join(sentences)), max_chars=70)

    assert [c.body for c in chunks] == [" ".join(sentences[i : i + 2]) for i in range(0, 6, 2)]


def test_never_cuts_a_sentence_even_when_it_exceeds_the_limit() -> None:
    sentence = "This single sentence is longer than the limit."

    assert [c.body for c in chunk_pages(_pages(sentence), max_chars=10)] == [sentence]
