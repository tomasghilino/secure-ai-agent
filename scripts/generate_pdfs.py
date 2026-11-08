"""Generate Northstar's synthetic enterprise PDFs from the YAML sources in data/source/.

Output:
    data/generated/pdfs/<tenant>/<doc_id>_v<version>.pdf
    data/generated/manifest.json   (document registry: metadata, sha256, page count)

Generation is deterministic: the same sources produce byte-identical PDFs, so their
sha256 stays stable and the ingestion pipeline can skip documents it already indexed.

Usage (from the repository root):
    uv run python -m scripts.generate_pdfs
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape

import yaml
from pydantic import BaseModel, ConfigDict, field_validator
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "data" / "source"
OUTPUT_DIR = ROOT / "data" / "generated"

BLOCK_TYPES = {"p", "bullets", "steps", "table", "note", "product_table"}


# --------------------------------------------------------------------------- source models
class Tenant(BaseModel):
    id: str
    legal_name: str
    currency: str
    page_size: Literal["letter", "a4"]


class Product(BaseModel):
    sku: str
    name: str
    category: str
    specs: str
    list_price_usd: int
    warranty_months: int
    lead_time_days: int
    returnable: bool


class Section(BaseModel):
    heading: str
    blocks: list[dict[str, Any]]

    @field_validator("blocks")
    @classmethod
    def _known_block_types(cls, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for block in blocks:
            if len(block) != 1 or next(iter(block)) not in BLOCK_TYPES:
                raise ValueError(f"block must have exactly one key in {sorted(BLOCK_TYPES)}")
        return blocks


class DocumentSource(BaseModel):
    """Front matter + body of one document version (the 'document registry' record)."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    title: str
    version: str
    effective_date: date
    status: Literal["active", "superseded", "draft"]
    supersedes: str | None = None
    tenant_id: str
    department: str
    doc_type: Literal["policy", "agreement", "sla", "catalog", "runbook", "handbook"]
    classification: Literal["public", "internal", "confidential", "restricted"]
    owner: str
    approved_by: str
    next_review: date
    sections: list[Section]

    @property
    def filename(self) -> str:
        return f"{self.doc_id}_v{self.version}.pdf"


# --------------------------------------------------------------------------- rendering
_STYLES = getSampleStyleSheet()
BODY = ParagraphStyle("Body", parent=_STYLES["BodyText"], fontSize=9.5, leading=13, spaceAfter=6)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.5, leading=11, spaceAfter=0)
CELL_HEADER = ParagraphStyle("CellHeader", parent=CELL, fontName="Helvetica-Bold")
TITLE = ParagraphStyle("Title", parent=_STYLES["Title"], fontSize=18, leading=22, spaceAfter=4)
SUBTITLE = ParagraphStyle("Subtitle", parent=BODY, textColor=colors.HexColor("#555555"))
HEADING = ParagraphStyle(
    "Heading", parent=_STYLES["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4
)
NOTE = ParagraphStyle(
    "Note",
    parent=BODY,
    backColor=colors.HexColor("#F2F5F9"),
    borderColor=colors.HexColor("#9DB2C8"),
    borderWidth=0.5,
    borderPadding=5,
    leftIndent=4,
    rightIndent=4,
    spaceBefore=4,
    spaceAfter=10,
)
BRAND = colors.HexColor("#1F3A5F")


def rich(text: Any) -> str:
    """Escape for ReportLab's mini-markup, then turn **bold** into <b>bold</b>."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(str(text)))


def make_table(
    columns: list[str], rows: list[list[Any]], widths: list[float], width: float
) -> Table:
    data = [[Paragraph(rich(c), CELL_HEADER) for c in columns]]
    data += [[Paragraph(rich(cell), CELL) for cell in row] for row in rows]
    table = Table(data, colWidths=[w * width for w in widths], repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE4EE")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9DB2C8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def render_block(block: dict[str, Any], products: list[Product], width: float) -> list[Flowable]:
    kind, value = next(iter(block.items()))
    if kind == "p":
        return [Paragraph(rich(value), BODY)]
    if kind == "note":
        return [Paragraph(f"<b>Note:</b> {rich(value)}", NOTE)]
    if kind in ("bullets", "steps"):
        items = [ListItem(Paragraph(rich(v), BODY), leftIndent=12) for v in value]
        bullet = "1" if kind == "steps" else "bullet"
        return [ListFlowable(items, bulletType=bullet, start="1" if kind == "steps" else None)]
    if kind == "table":
        widths = value.get("widths") or [1 / len(value["columns"])] * len(value["columns"])
        return [make_table(value["columns"], value["rows"], widths, width), Spacer(1, 8)]
    if kind == "product_table":
        rows = [
            [
                p.sku,
                p.name,
                p.specs,
                f"{p.list_price_usd:,}",
                str(p.warranty_months),
                str(p.lead_time_days),
                "Yes" if p.returnable else "No (build-to-order)",
            ]
            for p in products
            if p.category == value
        ]
        columns = [
            "SKU",
            "Product",
            "Key specifications",
            "List price (USD)",
            "Warranty (months)",
            "Lead time (days)",
            "Returnable",
        ]
        widths = [0.10, 0.18, 0.30, 0.11, 0.10, 0.09, 0.12]
        return [make_table(columns, rows, widths, width), Spacer(1, 8)]
    raise ValueError(f"unknown block type: {kind}")


def metadata_table(doc: DocumentSource, tenant: Tenant, width: float) -> Table:
    rows = [
        ["Document ID", doc.doc_id, "Version", doc.version],
        [
            "Effective date",
            doc.effective_date.isoformat(),
            "Classification",
            doc.classification.upper(),
        ],
        ["Department", doc.department, "Document type", doc.doc_type.title()],
        ["Owner", doc.owner, "Approved by", doc.approved_by],
        [
            "Supersedes",
            f"Version {doc.supersedes}" if doc.supersedes else "-",
            "Next review",
            doc.next_review.isoformat(),
        ],
        ["Entity", tenant.legal_name, "", ""],
    ]
    data = [
        [
            Paragraph(rich(a), CELL_HEADER),
            Paragraph(rich(b), CELL),
            Paragraph(rich(c), CELL_HEADER),
            Paragraph(rich(d), CELL),
        ]
        for a, b, c, d in rows
    ]
    table = Table(data, colWidths=[w * width for w in (0.17, 0.33, 0.17, 0.33)], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9DB2C8")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF2F7")),
                ("BACKGROUND", (2, 0), (2, -2), colors.HexColor("#EEF2F7")),
                ("SPAN", (1, -1), (3, -1)),
            ]
        )
    )
    return table


class NumberedCanvas(Canvas):  # type: ignore[misc]  # untyped base class
    """Draws header/footer with 'Page X of Y' (needs the total, so pages are buffered)."""

    def __init__(self, *args: Any, doc: DocumentSource, tenant: Tenant, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._source, self._tenant = doc, tenant
        self._pages: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        for state in self._pages:
            self.__dict__.update(state)
            self._draw_chrome(len(self._pages))
            super().showPage()
        super().save()

    def _draw_chrome(self, total: int) -> None:
        width, height = self._pagesize
        self.setStrokeColor(BRAND)
        self.setLineWidth(0.8)
        self.line(18 * mm, height - 15 * mm, width - 18 * mm, height - 15 * mm)
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(BRAND)
        self.drawString(18 * mm, height - 13 * mm, f"Northstar Industrial - {self._source.title}")
        self.drawRightString(
            width - 18 * mm,
            height - 13 * mm,
            f"{self._source.doc_id}  |  Version {self._source.version}",
        )
        self.setFont("Helvetica", 7.5)
        self.setFillColor(colors.HexColor("#555555"))
        self.line(18 * mm, 15 * mm, width - 18 * mm, 15 * mm)
        self.drawString(
            18 * mm,
            11 * mm,
            f"{self._tenant.legal_name}  |  Classification: {self._source.classification.upper()}"
            f"  |  Effective {self._source.effective_date.isoformat()}",
        )
        self.drawRightString(width - 18 * mm, 11 * mm, f"Page {self._pageNumber} of {total}")


def build_pdf(doc: DocumentSource, tenant: Tenant, products: list[Product], path: Path) -> None:
    pagesize = LETTER if tenant.page_size == "letter" else A4
    template = SimpleDocTemplate(
        str(path),
        pagesize=pagesize,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=22 * mm,
        bottomMargin=22 * mm,
        title=f"{doc.title} (v{doc.version})",
        author="Northstar Industrial (fictional)",
        subject=doc.doc_id,
        # Machine-readable metadata the ingestion pipeline can read back with PyMuPDF.
        keywords=(
            f"doc_id={doc.doc_id};version={doc.version};"
            f"effective_date={doc.effective_date.isoformat()};tenant_id={doc.tenant_id};"
            f"classification={doc.classification};doc_type={doc.doc_type}"
        ),
        creator="northstar-ai-operations-copilot/scripts/generate_pdfs.py",
    )
    width = template.width
    story: list[Flowable] = [
        Paragraph(rich(doc.title), TITLE),
        Paragraph(rich(f"{tenant.legal_name} - {doc.department}"), SUBTITLE),
        Spacer(1, 6),
        metadata_table(doc, tenant, width),
        Spacer(1, 10),
    ]
    for section in doc.sections:
        first, *rest = [f for block in section.blocks for f in render_block(block, products, width)]
        story.append(KeepTogether([Paragraph(rich(section.heading), HEADING), first]))
        story.extend(rest)
    template.build(story, canvasmaker=partial(NumberedCanvas, doc=doc, tenant=tenant))


# --------------------------------------------------------------------------- orchestration
def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def page_count(path: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", path.read_bytes()))


def generate(source_dir: Path = SOURCE_DIR, output_dir: Path = OUTPUT_DIR) -> list[dict[str, Any]]:
    rl_config.invariant = 1  # no timestamps / random IDs: byte-identical, reproducible output
    tenants = {t["id"]: Tenant(**t) for t in load_yaml(source_dir / "company.yaml")["tenants"]}
    products = [Product(**p) for p in load_yaml(source_dir / "products.yaml")["products"]]

    manifest: list[dict[str, Any]] = []
    for yaml_path in sorted((source_dir / "documents").rglob("*.yaml")):
        doc = DocumentSource(**load_yaml(yaml_path))
        tenant = tenants[doc.tenant_id]
        pdf_path = output_dir / "pdfs" / doc.tenant_id / doc.filename
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        build_pdf(doc, tenant, products, pdf_path)
        manifest.append(
            {
                **doc.model_dump(mode="json", exclude={"sections"}),
                "file": pdf_path.relative_to(output_dir).as_posix(),
                "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                "pages": page_count(pdf_path),
            }
        )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    manifest = generate()
    for entry in manifest:
        print(
            f"{entry['tenant_id']:9} {entry['doc_id']:16} v{entry['version']:7} "
            f"{entry['status']:10} {entry['pages']} pages  {entry['sha256'][:12]}  {entry['file']}"
        )
    print(f"\n{len(manifest)} documents -> {OUTPUT_DIR.relative_to(ROOT).as_posix()}/manifest.json")


if __name__ == "__main__":
    main()
