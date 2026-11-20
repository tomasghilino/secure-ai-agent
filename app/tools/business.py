"""Read-only business tools: inputs and implementations.

Every handler runs inside a tenant session (Row-Level Security) *and* filters by the
caller's tenant explicitly. Queries are built with SQLAlchemy expressions, so values are
always bound parameters, never SQL text. Results carry only the fields a task needs
(data minimisation) and lists are capped.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    CustomerRecord,
    InvoiceRecord,
    OrderLineRecord,
    OrderRecord,
    ProductRecord,
    SupplierRecord,
    SupportTicketRecord,
)
from app.rag.retrieval import SearchFilters, search_chunks
from app.tools.base import StrictInput, ToolContext, ToolError

ORDER_NUMBER = r"^SO-(EU-)?[0-9]{4}-[0-9]{4}$"
INVOICE_NUMBER = r"^INV-(EU-)?[0-9]{4}-[0-9]{4}$"
LIST_LIMIT = 20
POLICY_HITS = 5
POLICY_TEXT_CHARS = 1200


# --------------------------------------------------------------------------- inputs


class PolicySearch(StrictInput):
    query: str = Field(
        min_length=3, max_length=300, description="What to look for, in plain words."
    )
    as_of: date | None = Field(
        default=None,
        description="Only for questions about a past date: search the versions in force "
        "on that date (YYYY-MM-DD). Omit it for current rules.",
    )


class ProductLookup(StrictInput):
    sku: str = Field(pattern=r"^[A-Z]{2}-[0-9]{2,4}[A-Z]{0,2}$", description="e.g. NX-4400B")


class OrderLookup(StrictInput):
    order_number: str = Field(pattern=ORDER_NUMBER, description="e.g. SO-2026-0103")


class CustomerLookup(StrictInput):
    customer_number: str = Field(pattern=r"^C-[0-9]{4}$", description="e.g. C-1006")


class TicketLookup(StrictInput):
    ticket_number: str = Field(pattern=r"^T-[0-9]{4}$", description="e.g. T-5008")


class InvoiceLookup(StrictInput):
    invoice_number: str = Field(pattern=INVOICE_NUMBER, description="e.g. INV-2026-0106")


class OverdueInvoices(StrictInput):
    min_days_overdue: int = Field(default=1, ge=0, le=3650)


class SupplierLookup(StrictInput):
    supplier_code: str = Field(pattern=r"^SUP-[A-Z0-9]{3,5}$", description="e.g. SUP-0007")


# --------------------------------------------------------------------------- helpers


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def _minutes_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int((end - start).total_seconds() // 60)


async def load_order(
    session: AsyncSession, tenant_id: str, order_number: str
) -> tuple[OrderRecord, CustomerRecord]:
    row = (
        await session.execute(
            select(OrderRecord, CustomerRecord)
            .join(CustomerRecord, OrderRecord.customer_id == CustomerRecord.id)
            .where(OrderRecord.tenant_id == tenant_id, OrderRecord.order_number == order_number)
        )
    ).one_or_none()
    if row is None:
        raise ToolError(f"order {order_number} not found")
    order, customer = row
    return order, customer


async def describe_order(
    session: AsyncSession, order: OrderRecord, customer: CustomerRecord, today: date
) -> dict[str, Any]:
    lines = await session.execute(
        select(OrderLineRecord, ProductRecord)
        .join(ProductRecord, OrderLineRecord.product_id == ProductRecord.id)
        .where(OrderLineRecord.order_id == order.id)
        .order_by(OrderLineRecord.line_number)
    )
    tickets = await session.scalars(
        select(SupportTicketRecord.ticket_number)
        .where(SupportTicketRecord.order_id == order.id)
        .order_by(SupportTicketRecord.ticket_number)
    )
    return {
        "order_number": order.order_number,
        "status": order.status,
        "customer": {
            "customer_number": customer.customer_number,
            "name": customer.name,
            "support_tier": customer.support_tier,
        },
        "order_date": order.order_date,
        "delivered_date": order.delivered_date,
        "days_since_delivery": (
            (today - order.delivered_date).days if order.delivered_date else None
        ),
        "currency": order.currency,
        "total_amount": order.total_amount,
        "lines": [
            {
                "line_number": line.line_number,
                "sku": product.sku,
                "product": product.name,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "final_sale": line.final_sale,
                "product_returnable": product.returnable,
                "warranty_months": product.warranty_months,
            }
            for line, product in lines
        ],
        "tickets": list(tickets),
    }


# --------------------------------------------------------------------------- handlers


async def search_policies(
    session: AsyncSession, args: PolicySearch, ctx: ToolContext
) -> dict[str, Any]:
    hits = await search_chunks(
        session,
        ctx.embeddings,
        args.query,
        tenant_id=ctx.principal.tenant_id,
        limit=POLICY_HITS,
        mode=ctx.retrieval_mode,
        filters=SearchFilters(as_of=args.as_of),
    )
    return {
        "results": [
            {
                "doc_key": hit.doc_key,
                "version": hit.version,
                "title": hit.title,
                "section": hit.section_path[-1] if hit.section_path else "",
                "page": hit.page_number,
                "text": _clip(hit.content, POLICY_TEXT_CHARS),
            }
            for hit in hits
        ]
    }


async def get_product(
    session: AsyncSession, args: ProductLookup, ctx: ToolContext
) -> dict[str, Any]:
    product = await session.scalar(
        select(ProductRecord).where(
            ProductRecord.tenant_id == ctx.principal.tenant_id, ProductRecord.sku == args.sku
        )
    )
    if product is None:
        raise ToolError(f"product {args.sku} not found")
    return {
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "specs": product.specs,
        "list_price": product.list_price,
        "currency": product.currency,
        "warranty_months": product.warranty_months,
        "lead_time_days": product.lead_time_days,
        "returnable": product.returnable,
    }


async def get_order(session: AsyncSession, args: OrderLookup, ctx: ToolContext) -> dict[str, Any]:
    order, customer = await load_order(session, ctx.principal.tenant_id, args.order_number)
    return await describe_order(session, order, customer, ctx.today)


async def get_customer(
    session: AsyncSession, args: CustomerLookup, ctx: ToolContext
) -> dict[str, Any]:
    tenant_id = ctx.principal.tenant_id
    customer = await session.scalar(
        select(CustomerRecord).where(
            CustomerRecord.tenant_id == tenant_id,
            CustomerRecord.customer_number == args.customer_number,
        )
    )
    if customer is None:
        raise ToolError(f"customer {args.customer_number} not found")
    orders = await session.execute(
        select(OrderRecord.order_number, OrderRecord.status, OrderRecord.order_date)
        .where(OrderRecord.customer_id == customer.id)
        .order_by(OrderRecord.order_date.desc())
        .limit(LIST_LIMIT)
    )
    open_tickets = await session.scalars(
        select(SupportTicketRecord.ticket_number)
        .where(
            SupportTicketRecord.customer_id == customer.id,
            SupportTicketRecord.status.in_(("open", "in_progress")),
        )
        .order_by(SupportTicketRecord.ticket_number)
        .limit(LIST_LIMIT)
    )
    return {
        "customer_number": customer.customer_number,
        "name": customer.name,
        "segment": customer.segment,
        "country": customer.country,
        "support_tier": customer.support_tier,
        "recent_orders": [
            {"order_number": number, "status": status, "order_date": ordered}
            for number, status, ordered in orders
        ],
        "open_tickets": list(open_tickets),
    }


async def get_ticket(session: AsyncSession, args: TicketLookup, ctx: ToolContext) -> dict[str, Any]:
    row = (
        await session.execute(
            select(SupportTicketRecord, CustomerRecord)
            .join(CustomerRecord, SupportTicketRecord.customer_id == CustomerRecord.id)
            .where(
                SupportTicketRecord.tenant_id == ctx.principal.tenant_id,
                SupportTicketRecord.ticket_number == args.ticket_number,
            )
        )
    ).one_or_none()
    if row is None:
        raise ToolError(f"ticket {args.ticket_number} not found")
    ticket, customer = row
    order_number = (
        await session.scalar(
            select(OrderRecord.order_number).where(OrderRecord.id == ticket.order_id)
        )
        if ticket.order_id
        else None
    )
    sku = (
        await session.scalar(select(ProductRecord.sku).where(ProductRecord.id == ticket.product_id))
        if ticket.product_id
        else None
    )
    return {
        "ticket_number": ticket.ticket_number,
        # Free text written by a customer: untrusted, may contain instructions (see T-5009).
        "subject": ticket.subject,
        "priority": ticket.priority,
        "status": ticket.status,
        "customer": {
            "customer_number": customer.customer_number,
            "name": customer.name,
            "support_tier": customer.support_tier,
        },
        "order_number": order_number,
        "sku": sku,
        "opened_at": ticket.opened_at,
        "first_response_at": ticket.first_response_at,
        "first_response_minutes": _minutes_between(ticket.opened_at, ticket.first_response_at),
        "resolved_at": ticket.resolved_at,
    }


def _days_overdue(invoice: InvoiceRecord, today: date) -> int:
    if invoice.status != "open" or today <= invoice.due_date:
        return 0
    return (today - invoice.due_date).days


def _describe_invoice(
    invoice: InvoiceRecord, order_number: str, customer: CustomerRecord, today: date
) -> dict[str, Any]:
    return {
        "invoice_number": invoice.invoice_number,
        "order_number": order_number,
        "customer": {"customer_number": customer.customer_number, "name": customer.name},
        "amount": invoice.amount,
        "currency": invoice.currency,
        "status": invoice.status,
        "issue_date": invoice.issue_date,
        "due_date": invoice.due_date,
        "paid_date": invoice.paid_date,
        "days_overdue": _days_overdue(invoice, today),
    }


def _invoice_query() -> Any:
    return (
        select(InvoiceRecord, OrderRecord.order_number, CustomerRecord)
        .join(OrderRecord, InvoiceRecord.order_id == OrderRecord.id)
        .join(CustomerRecord, InvoiceRecord.customer_id == CustomerRecord.id)
    )


async def get_invoice(
    session: AsyncSession, args: InvoiceLookup, ctx: ToolContext
) -> dict[str, Any]:
    row = (
        await session.execute(
            _invoice_query().where(
                InvoiceRecord.tenant_id == ctx.principal.tenant_id,
                InvoiceRecord.invoice_number == args.invoice_number,
            )
        )
    ).one_or_none()
    if row is None:
        raise ToolError(f"invoice {args.invoice_number} not found")
    invoice, order_number, customer = row
    return _describe_invoice(invoice, order_number, customer, ctx.today)


async def list_overdue_invoices(
    session: AsyncSession, args: OverdueInvoices, ctx: ToolContext
) -> dict[str, Any]:
    rows = await session.execute(
        _invoice_query()
        .where(
            InvoiceRecord.tenant_id == ctx.principal.tenant_id,
            InvoiceRecord.status == "open",
            InvoiceRecord.due_date < ctx.today,
        )
        .order_by(InvoiceRecord.due_date)
        .limit(LIST_LIMIT)
    )
    invoices = [
        _describe_invoice(invoice, order_number, customer, ctx.today)
        for invoice, order_number, customer in rows
    ]
    return {
        "as_of": ctx.today,
        "invoices": [i for i in invoices if i["days_overdue"] >= args.min_days_overdue],
    }


async def get_supplier(
    session: AsyncSession, args: SupplierLookup, ctx: ToolContext
) -> dict[str, Any]:
    supplier = await session.scalar(
        select(SupplierRecord).where(
            SupplierRecord.tenant_id == ctx.principal.tenant_id,
            SupplierRecord.supplier_code == args.supplier_code,
        )
    )
    if supplier is None:
        raise ToolError(f"supplier {args.supplier_code} not found")
    return {
        "supplier_code": supplier.supplier_code,
        "name": supplier.name,
        "category": supplier.category,
        "country": supplier.country,
        "status": supplier.status,
        "risk_rating": supplier.risk_rating,
        "annual_spend": supplier.annual_spend,
        "currency": supplier.currency,
        "contract_doc_key": supplier.contract_doc_key,
    }
