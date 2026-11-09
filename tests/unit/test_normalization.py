from app.ingestion.extraction import ExtractedPage, extract_pdf
from app.ingestion.normalization import clean_text, normalize_pages


def _pages(*texts: str) -> list[ExtractedPage]:
    return [ExtractedPage(page_number=i + 1, text=text) for i, text in enumerate(texts)]


def test_clean_text_removes_extraction_artifacts() -> None:
    raw = (
        "# **Returns Policy** \r\n\r\n\r\n\r\n"
        "Submit within **30 days** .\u00a0Thanks\u200b!\n"
        "|RMA|Issued by<br>Customer   Operations|"
    )

    assert clean_text(raw) == (
        "# Returns Policy\n\nSubmit within 30 days. Thanks!\n|RMA|Issued by Customer Operations|"
    )


def test_clean_text_keeps_list_indentation() -> None:
    assert clean_text("- parent\n  - nested    item") == "- parent\n  - nested item"


def test_removes_running_headers_and_footers_but_keeps_page_numbers() -> None:
    template = "**ACME-POL-001 | Version 2.0**\n\n**Acme - Travel Policy**\n\n{body}\n\n"
    template += "Acme Corp | Internal\n\nPage {n} of 3"
    bodies = [
        "## 1. Scope\n\nApplies to all staff.",
        "## 2. Meals\n\nDaily limit is 60 USD.",
        "## 3. Hotels\n\nBook through the portal.",
    ]
    pages = _pages(*(template.format(body=b, n=i + 1) for i, b in enumerate(bodies)))

    result = normalize_pages(pages)

    assert [p.text for p in result.pages] == bodies
    assert [p.page_number for p in result.pages] == [1, 2, 3]
    assert result.removed_boilerplate == (
        "ACME-POL-001 | Version 2.0",
        "Acme - Travel Policy",
        "Acme Corp | Internal",
        "Page 1 of 3",
    )


def test_keeps_table_header_repeated_on_a_continuation_page() -> None:
    pages = _pages(
        "Warranty terms.\n\n|SKU|Months|\n|---|---|\n|NX-4400|24|",
        "|SKU|Months|\n|---|---|\n|NX-4400B|36|\n\nEnd of table.",
    )

    result = normalize_pages(pages)

    assert result.pages[1].text.startswith("|SKU|Months|\n|---|---|")
    assert result.removed_boilerplate == ()


def test_only_lines_near_the_top_or_bottom_can_be_boilerplate() -> None:
    body = "alpha\n\nbravo\n\nSee the appendix.\n\ncharlie\n\ndelta"
    other = "echo\n\nfoxtrot\n\nSee the appendix.\n\ngolf\n\nhotel"
    pages = _pages(f"Header\n\n{body}\n\nFooter", f"Header\n\n{other}\n\nFooter")

    result = normalize_pages(pages)

    assert all("See the appendix." in p.text for p in result.pages)
    assert result.removed_boilerplate == ("Header", "Footer")


def test_single_page_document_only_loses_its_page_number() -> None:
    result = normalize_pages(_pages("**Acme - Memo**\n\nBody text.\n\nPage 1 of 1"))

    assert result.pages[0].text == "Acme - Memo\n\nBody text."


def test_normalizes_the_generated_returns_policy(returns_policy_pdf: bytes) -> None:
    result = normalize_pages(extract_pdf(returns_policy_pdf).pages)
    text = "\n".join(p.text for p in result.pages)

    assert "Northstar Industrial - Returns and Refunds Policy" not in text  # running header
    assert "Page 1 of 3" not in text  # footer
    assert "**" not in text
    assert "<br>" not in text
    assert "# Returns and Refunds Policy" in text  # the real title heading survives
    assert "30 calendar days of the delivery date." in text
    assert result.pages[1].text.startswith("## 4. Return Process")
