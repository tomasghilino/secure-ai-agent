"""Who may use which tool: a deny-by-default role-by-tool matrix (OWASP LLM06, ASI03).

The model never decides permissions. It only sees the tools its user's role allows, and
every call is checked again here before it runs, because a model can name a tool it was
never shown.

Identity (tenant, user, role) comes from the authenticated request or the MCP server's
launch configuration, never from tool arguments: a model cannot act for someone else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast, get_args

Role = Literal["support_agent", "finance_analyst", "supervisor", "editor"]
ROLES: tuple[Role, ...] = get_args(Role)

READ_POLICIES = "search_policies"
REQUEST_RETURN = "request_return_authorization"

_SHARED = frozenset({READ_POLICIES, "get_product", "get_order", "get_customer"})
ROLE_TOOLS: dict[Role, frozenset[str]] = {
    # Customer support: orders and tickets; can *propose* a return authorization.
    "support_agent": _SHARED | {"get_ticket", REQUEST_RETURN},
    # Finance: invoices and suppliers; no tickets, no actions.
    "finance_analyst": _SHARED | {"get_invoice", "list_overdue_invoices", "get_supplier"},
    # Supervisors see everything and are the only ones who approve actions.
    "supervisor": _SHARED
    | {"get_ticket", "get_invoice", "list_overdue_invoices", "get_supplier", REQUEST_RETURN},
    # Knowledge-base editors upload policies and check what is indexed; no business data.
    "editor": frozenset({READ_POLICIES}),
}
APPROVER_ROLES: frozenset[Role] = frozenset({"supervisor"})
# Who may add documents to the knowledge base (OWASP LLM04, data poisoning). Not the
# approvers: whoever approves returns cannot also rewrite the returns policy.
DOCUMENT_EDITOR_ROLES: frozenset[Role] = frozenset({"editor"})


@dataclass(frozen=True, slots=True)
class Principal:
    """The identity a request, an agent run or an MCP session acts as."""

    tenant_id: str
    user_id: str
    role: Role


def parse_role(value: str) -> Role | None:
    """``value`` as a known role, or None."""
    return value if value in ROLES else None


def is_allowed(role: str, tool_name: str) -> bool:
    """Unknown roles and unknown tools are denied (deny by default)."""
    return tool_name in ROLE_TOOLS.get(cast(Role, role), frozenset())


def approval_denied_reason(approver: Principal, requested_by: str) -> str | None:
    """Why ``approver`` may not approve a request made by ``requested_by``, or None."""
    if approver.role not in APPROVER_ROLES:
        return f"role {approver.role} cannot approve actions"
    if approver.user_id == requested_by:
        return "an action cannot be approved by the person who requested it"
    return None
