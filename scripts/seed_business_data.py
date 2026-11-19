"""Load the business records of data/source/business into PostgreSQL (`make seed`).

Needs `make up` and `make migrate`. Usage (from the repository root):
    uv run python -m scripts.seed_business_data

Idempotent: each run replaces every tenant's business rows with the source files, in one
transaction. IDs are derived from the tenant and the natural key (UUID v5), so a record
keeps the same ID across runs. Runs as the table owner: seeding is an admin task, not a
tenant request.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.database.models import (
    Base,
    CustomerRecord,
    InvoiceRecord,
    OrderLineRecord,
    OrderRecord,
    ProductRecord,
    SupplierRecord,
    SupportTicketRecord,
)
from app.database.session import build_engine, build_session_factory
from scripts.generate_pdfs import SOURCE_DIR, Product, load_yaml

BUSINESS_DIR = SOURCE_DIR / "business"
# Fixed namespace for UUID v5: the same tenant + key always gives the same ID.
ID_NAMESPACE = uuid.UUID("6f1c2d4e-8a3b-4c5d-9e7f-0a1b2c3d4e5f")
# Children first: deleting in this order never breaks a foreign key.
TABLES_IN_DELETE_ORDER: tuple[type[Base], ...] = (
    OrderLineRecord,
    SupportTicketRecord,
    InvoiceRecord,
    OrderRecord,
    CustomerRecord,
    ProductRecord,
    SupplierRecord,
)


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductPrice(_Source):
    sku: str
    list_price: Decimal


class Customer(_Source):
    number: str
    name: str
    segment: str
    country: str
    support_tier: Literal["standard", "premium", "platinum"]


class OrderLine(_Source):
    sku: str
    quantity: int = Field(gt=0)
    unit_price: Decimal | None = None  # default: the product's list price
    final_sale: bool = False


class Order(_Source):
    number: str
    customer: str
    status: Literal["confirmed", "shipped", "delivered", "cancelled"]
    order_date: date
    delivered_date: date | None = None
    lines: list[OrderLine] = Field(min_length=1)

    @model_validator(mode="after")
    def _delivered_date_only_when_delivered(self) -> Self:
        if (self.status == "delivered") != (self.delivered_date is not None):
            raise ValueError(f"{self.number}: delivered_date is required exactly when delivered")
        return self


class Invoice(_Source):
    number: str
    order: str
    issue_date: date
    due_date: date
    status: Literal["open", "paid", "void"]
    paid_date: date | None = None


class Ticket(_Source):
    number: str
    customer: str
    order: str | None = None
    sku: str | None = None
    priority: Literal["P1", "P2", "P3", "P4"]
    status: Literal["open", "in_progress", "resolved", "closed"]
    subject: str
    opened_at: datetime
    first_response_at: datetime | None = None
    resolved_at: datetime | None = None


class Supplier(_Source):
    code: str
    name: str
    category: str
    country: str
    status: Literal["approved", "pending", "suspended"]
    risk_rating: Literal["low", "medium", "high"]
    annual_spend: Decimal
    contract_doc_key: str | None = None


class TenantBusinessData(_Source):
    currency: str
    products: list[ProductPrice] | None = None  # None: the whole catalog at USD list prices
    customers: list[Customer]
    orders: list[Order]
    invoices: list[Invoice]
    tickets: list[Ticket]
    suppliers: list[Supplier]


Rows = dict[type[Base], list[dict[str, Any]]]


def stable_id(tenant_id: str, kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(ID_NAMESPACE, f"{tenant_id}/{kind}/{key}")


def build_rows(tenant_id: str, data: TenantBusinessData, catalog: list[Product]) -> Rows:
    """Turn one tenant's source file into table rows; fails on any dangling reference."""
    by_sku = {product.sku: product for product in catalog}
    prices = (
        {p.sku: p.list_price for p in data.products}
        if data.products is not None
        else {p.sku: Decimal(p.list_price_usd) for p in catalog}
    )
    unknown = set(prices) - set(by_sku)
    if unknown:
        raise ValueError(f"{tenant_id}: unknown SKUs {sorted(unknown)}")

    def ref(kind: str, key: str, known: set[str]) -> uuid.UUID:
        if key not in known:
            raise ValueError(f"{tenant_id}: unknown {kind} {key!r}")
        return stable_id(tenant_id, kind, key)

    customers = {c.number for c in data.customers}
    orders = {o.number for o in data.orders}
    rows: Rows = {
        ProductRecord: [
            {
                "id": stable_id(tenant_id, "product", sku),
                "tenant_id": tenant_id,
                "sku": sku,
                "name": by_sku[sku].name,
                "category": by_sku[sku].category,
                "specs": by_sku[sku].specs,
                "list_price": price,
                "currency": data.currency,
                "warranty_months": by_sku[sku].warranty_months,
                "lead_time_days": by_sku[sku].lead_time_days,
                "returnable": by_sku[sku].returnable,
            }
            for sku, price in prices.items()
        ],
        CustomerRecord: [
            {
                "id": stable_id(tenant_id, "customer", c.number),
                "tenant_id": tenant_id,
                "customer_number": c.number,
                "name": c.name,
                "segment": c.segment,
                "country": c.country,
                "support_tier": c.support_tier,
            }
            for c in data.customers
        ],
        OrderRecord: [],
        OrderLineRecord: [],
        InvoiceRecord: [],
        SupportTicketRecord: [],
        SupplierRecord: [
            {
                "id": stable_id(tenant_id, "supplier", s.code),
                "tenant_id": tenant_id,
                "supplier_code": s.code,
                "name": s.name,
                "category": s.category,
                "country": s.country,
                "status": s.status,
                "risk_rating": s.risk_rating,
                "annual_spend": s.annual_spend,
                "currency": data.currency,
                "contract_doc_key": s.contract_doc_key,
            }
            for s in data.suppliers
        ],
    }

    totals: dict[str, Decimal] = {}
    for order in data.orders:
        order_id = stable_id(tenant_id, "order", order.number)
        total = Decimal(0)
        for number, line in enumerate(order.lines, start=1):
            unit_price = line.unit_price if line.unit_price is not None else prices[line.sku]
            total += unit_price * line.quantity
            rows[OrderLineRecord].append(
                {
                    "id": stable_id(tenant_id, "order_line", f"{order.number}/{number}"),
                    "tenant_id": tenant_id,
                    "order_id": order_id,
                    "line_number": number,
                    "product_id": ref("product", line.sku, set(prices)),
                    "quantity": line.quantity,
                    "unit_price": unit_price,
                    "final_sale": line.final_sale,
                }
            )
        totals[order.number] = total
        rows[OrderRecord].append(
            {
                "id": order_id,
                "tenant_id": tenant_id,
                "order_number": order.number,
                "customer_id": ref("customer", order.customer, customers),
                "status": order.status,
                "order_date": order.order_date,
                "delivered_date": order.delivered_date,
                "currency": data.currency,
                "total_amount": total,
            }
        )

    customer_of = {o.number: o.customer for o in data.orders}
    for invoice in data.invoices:
        rows[InvoiceRecord].append(
            {
                "id": stable_id(tenant_id, "invoice", invoice.number),
                "tenant_id": tenant_id,
                "invoice_number": invoice.number,
                "order_id": ref("order", invoice.order, orders),
                "customer_id": stable_id(tenant_id, "customer", customer_of[invoice.order]),
                "issue_date": invoice.issue_date,
                "due_date": invoice.due_date,
                "amount": totals[invoice.order],
                "currency": data.currency,
                "status": invoice.status,
                "paid_date": invoice.paid_date,
            }
        )
    for ticket in data.tickets:
        rows[SupportTicketRecord].append(
            {
                "id": stable_id(tenant_id, "ticket", ticket.number),
                "tenant_id": tenant_id,
                "ticket_number": ticket.number,
                "customer_id": ref("customer", ticket.customer, customers),
                "order_id": ref("order", ticket.order, orders) if ticket.order else None,
                "product_id": ref("product", ticket.sku, set(prices)) if ticket.sku else None,
                "priority": ticket.priority,
                "status": ticket.status,
                "subject": ticket.subject,
                "opened_at": ticket.opened_at,
                "first_response_at": ticket.first_response_at,
                "resolved_at": ticket.resolved_at,
            }
        )
    return rows


def load_sources(business_dir: Path = BUSINESS_DIR) -> dict[str, Rows]:
    catalog = [Product(**p) for p in load_yaml(SOURCE_DIR / "products.yaml")["products"]]
    return {
        path.stem: build_rows(path.stem, TenantBusinessData(**load_yaml(path)), catalog)
        for path in sorted(business_dir.glob("*.yaml"))
    }


async def seed(session: AsyncSession, rows_by_tenant: dict[str, Rows]) -> None:
    async with session.begin():  # all tenants, all tables, or nothing
        for table in TABLES_IN_DELETE_ORDER:
            await session.execute(
                delete(table).where(table.__table__.c.tenant_id.in_(rows_by_tenant))
            )
        for rows in rows_by_tenant.values():
            for table in reversed(TABLES_IN_DELETE_ORDER):  # parents first
                if rows[table]:
                    await session.execute(insert(table), rows[table])


async def main() -> None:
    rows_by_tenant = load_sources()
    engine = build_engine(get_settings().database_url)
    try:
        async with build_session_factory(engine)() as session:
            await seed(session, rows_by_tenant)
    finally:
        await engine.dispose()
    for tenant_id, rows in rows_by_tenant.items():
        counts = ", ".join(f"{len(r)} {t.__tablename__}" for t, r in rows.items())
        print(f"{tenant_id}: {counts}")


if __name__ == "__main__":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
