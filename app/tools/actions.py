"""The one write the agent can lead to: a return authorization (RMA), after human approval.

Flow (AWS Agentic Scope 2, "prescribed agency"):

1. The model calls ``request_return_authorization``. Nothing is written: ``propose``
   validates the arguments, checks the order in the database and returns a *pending
   action* with facts read from the database (not the model's summary), so the approver
   decides on real data (OWASP ASI09).
2. The graph pauses (LangGraph ``interrupt``) until a supervisor decides.
3. ``decide`` re-checks who is approving, re-reads the order (it may have changed while
   waiting) and only then inserts the RMA. One RMA per order: approving twice is harmless.

The eligibility rules that can be decided from data are enforced in code at both steps
(``_eligible_order``): delivered, inside the tenant's return window, no build-to-order or
final-sale lines, no existing RMA. The red-team suite showed why: a hijacked model plus
an approver who does not read would otherwise create an RMA for a non-returnable pump.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OrderRecord, ReturnAuthorizationRecord
from app.database.tenancy import tenant_session
from app.tools.audit import record_audit
from app.tools.authorization import (
    REQUEST_RETURN,
    Principal,
    Role,
    approval_denied_reason,
    is_allowed,
)
from app.tools.base import StrictInput, ToolContext, ToolError, ToolSpec, to_json
from app.tools.business import ORDER_NUMBER, describe_order, load_order


class ReturnAuthorizationInput(StrictInput):
    order_number: str = Field(pattern=ORDER_NUMBER, description="e.g. SO-2026-0101")
    reason: str = Field(
        min_length=5, max_length=500, description="Why the customer returns it, as stated."
    )


async def _not_executable(*_: Any) -> None:
    raise ToolError("request_return_authorization only runs after human approval")


# The model sees this like any other tool; calling it only creates a proposal.
REQUEST_RETURN_SPEC = ToolSpec(
    REQUEST_RETURN,
    "Propose a return authorization (RMA) for a delivered order. Nothing happens until a "
    "supervisor approves it. Only call it when the user asks for a return and the active "
    "returns policy allows it.",
    ReturnAuthorizationInput,
    _not_executable,
)


class ApprovalDecision(BaseModel):
    """What a supervisor sends to resume a paused run."""

    approved: bool
    approver_id: str
    approver_role: Role
    comment: str | None = Field(default=None, max_length=500)


async def _eligible_order(
    session: AsyncSession, ctx: ToolContext, order_number: str
) -> tuple[OrderRecord, dict[str, Any]]:
    """The order and its facts, if a return is allowed by rules decidable from data."""
    tenant_id = ctx.principal.tenant_id
    order, customer = await load_order(session, tenant_id, order_number)
    if order.status != "delivered":
        raise ToolError(f"order {order_number} is {order.status}: only delivered orders return")
    facts = await describe_order(session, order, customer, ctx.today)
    blocked = [
        line["sku"]
        for line in facts["lines"]
        if line["final_sale"] or not line["product_returnable"]
    ]
    if blocked:
        raise ToolError(
            f"order {order_number} has lines that are not returnable "
            f"(build-to-order or final sale): {', '.join(blocked)}"
        )
    if ctx.return_window_days is None:
        raise ToolError("no return window is configured for this tenant")
    days = facts["days_since_delivery"]
    if days is None or days > ctx.return_window_days:
        raise ToolError(
            f"order {order_number} was delivered {days} days ago: outside the "
            f"{ctx.return_window_days}-day return window"
        )
    existing = await session.scalar(
        select(ReturnAuthorizationRecord.rma_number).where(
            ReturnAuthorizationRecord.tenant_id == tenant_id,
            ReturnAuthorizationRecord.order_id == order.id,
        )
    )
    if existing:
        raise ToolError(f"order {order_number} already has return authorization {existing}")
    return order, facts


async def propose(
    ctx: ToolContext, tool_call_id: str, arguments: Any, *, thread_id: uuid.UUID | None
) -> dict[str, Any]:
    """A pending action ready for approval, or ``{"status": ..., "error": ...}``."""
    principal = ctx.principal
    outcome: dict[str, Any]
    if not is_allowed(principal.role, REQUEST_RETURN):
        outcome = {"status": "denied", "error": f"role {principal.role} may not request returns"}
    else:
        try:
            args = ReturnAuthorizationInput.model_validate(arguments)
            async with (
                tenant_session(ctx.sessions, principal.tenant_id) as session,
                session.begin(),
            ):
                _, facts = await _eligible_order(session, ctx, args.order_number)
        except ValidationError as exc:
            outcome = {"status": "error", "error": f"invalid arguments: {exc.error_count()} errors"}
        except ToolError as exc:
            outcome = {"status": "error", "error": str(exc)}
        else:
            outcome = {
                "status": "pending_approval",
                "tool_call_id": tool_call_id,
                "tool": REQUEST_RETURN,
                "arguments": args.model_dump(),
                "requested_by": principal.user_id,
                "requested_by_role": principal.role,
                "proposed_at": datetime.now(UTC).isoformat(),
                # From the database, JSON-safe: what the approver should look at.
                "facts": json.loads(to_json(facts)),
            }
    await record_audit(
        ctx.sessions,
        principal,
        "action_proposed",
        outcome=outcome["status"],
        tool_name=REQUEST_RETURN,
        arguments=arguments,
        detail=outcome.get("error"),
        thread_id=thread_id,
    )
    return outcome


DecisionStatus = Literal["executed", "rejected", "denied", "error"]


async def decide(
    ctx: ToolContext,
    pending: dict[str, Any],
    decision: ApprovalDecision,
    *,
    thread_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Apply a supervisor's decision. ``ctx.principal`` is the *requester*."""
    approver = Principal(ctx.principal.tenant_id, decision.approver_id, decision.approver_role)
    requested_by = str(pending["requested_by"])
    denied = approval_denied_reason(approver, requested_by)
    if denied is not None:  # also checked by the API; this is defence in depth
        await record_audit(
            ctx.sessions,
            approver,
            "action_decided",
            outcome="denied",
            tool_name=REQUEST_RETURN,
            detail=denied,
            thread_id=thread_id,
        )
        return {"status": "denied", "error": denied}

    await record_audit(
        ctx.sessions,
        approver,
        "action_decided",
        outcome="approved" if decision.approved else "rejected",
        tool_name=REQUEST_RETURN,
        arguments=pending["arguments"],
        detail=decision.comment,
        thread_id=thread_id,
    )
    if not decision.approved:
        return {"status": "rejected", "comment": decision.comment}

    args = ReturnAuthorizationInput.model_validate(pending["arguments"])
    result: dict[str, Any]
    try:
        async with (
            tenant_session(ctx.sessions, ctx.principal.tenant_id) as session,
            session.begin(),
        ):
            order, _ = await _eligible_order(session, ctx, args.order_number)
            rma_number = f"RMA-{args.order_number}"
            session.add(
                ReturnAuthorizationRecord(
                    tenant_id=ctx.principal.tenant_id,
                    rma_number=rma_number,
                    order_id=order.id,
                    reason=args.reason,
                    requested_by=requested_by,
                    approved_by=approver.user_id,
                    thread_id=thread_id,
                )
            )
    except ToolError as exc:
        result = {"status": "error", "error": str(exc)}
    else:
        result = {"status": "executed", "rma_number": rma_number}
    await record_audit(
        ctx.sessions,
        approver,
        "action_executed",
        outcome=result["status"],
        tool_name=REQUEST_RETURN,
        arguments=pending["arguments"],
        detail=result.get("error") or result.get("rma_number"),
        thread_id=thread_id,
    )
    return result
