"""The tool catalog: the single list of read-only tools, shared by the agent and MCP.

Descriptions are part of the security surface: the model reads them as instructions.
They are reviewed like code, and the MCP client pins their hash (see ``ToolSpec``).
"""

from __future__ import annotations

from app.tools import business
from app.tools.authorization import ROLE_TOOLS, Role
from app.tools.base import ToolSpec

READ_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_policies",
        "Search the company's policy documents (returns, warranty, SLA, procurement, "
        "travel...). Returns the most relevant passages with document, version and section.",
        business.PolicySearch,
        business.search_policies,
    ),
    ToolSpec(
        "get_product",
        "Look up a product by SKU: specs, list price, warranty months, returnable flag.",
        business.ProductLookup,
        business.get_product,
    ),
    ToolSpec(
        "get_order",
        "Look up a sales order: status, customer, delivery date, days since delivery and "
        "lines (final-sale and returnable flags, warranty).",
        business.OrderLookup,
        business.get_order,
    ),
    ToolSpec(
        "get_customer",
        "Look up a customer: segment, country, support tier, recent orders, open tickets.",
        business.CustomerLookup,
        business.get_customer,
    ),
    ToolSpec(
        "get_ticket",
        "Look up a support ticket: subject, priority, status, timestamps and first-response "
        "time in minutes.",
        business.TicketLookup,
        business.get_ticket,
    ),
    ToolSpec(
        "get_invoice",
        "Look up an invoice: amount, status, due date, paid date and days overdue.",
        business.InvoiceLookup,
        business.get_invoice,
    ),
    ToolSpec(
        "list_overdue_invoices",
        "List open invoices past their due date, oldest first (at most 20).",
        business.OverdueInvoices,
        business.list_overdue_invoices,
    ),
    ToolSpec(
        "get_supplier",
        "Look up a supplier: status (approved, pending, suspended), risk rating, annual "
        "spend and contract document.",
        business.SupplierLookup,
        business.get_supplier,
    ),
)
CATALOG: dict[str, ToolSpec] = {spec.name: spec for spec in READ_TOOLS}


def read_tools_for(role: Role) -> list[ToolSpec]:
    """The read-only tools a role may use, in catalog order."""
    return [spec for spec in READ_TOOLS if spec.name in ROLE_TOOLS[role]]
