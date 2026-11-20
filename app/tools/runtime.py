"""Run one read-only tool call: authorize → validate → execute (tenant session) → audit.

The agent (in-process) and the MCP server both call ``execute_tool``, so the checks are
identical whatever the transport.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError

from app.core.logging import get_logger
from app.database.tenancy import tenant_session
from app.tools.audit import record_audit
from app.tools.authorization import is_allowed
from app.tools.base import ToolContext, ToolError, ToolResult
from app.tools.catalog import CATALOG

log = get_logger(__name__)


def _validation_summary(exc: ValidationError) -> str:
    # Field names and messages only: never echo the (untrusted) input values back.
    return "; ".join(
        f"{'.'.join(str(p) for p in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors(include_input=False)
    )


async def _run(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> ToolResult:
    spec = CATALOG.get(name)
    if spec is None:
        return ToolResult(name, "denied", error=f"unknown tool {name}")
    if not is_allowed(ctx.principal.role, name):
        return ToolResult(name, "denied", error=f"role {ctx.principal.role} may not use {name}")
    try:
        args = spec.input_model.model_validate(arguments)
    except ValidationError as exc:
        return ToolResult(name, "error", error=f"invalid arguments: {_validation_summary(exc)}")
    try:
        async with (
            tenant_session(ctx.sessions, ctx.principal.tenant_id) as session,
            session.begin(),
        ):
            data = await spec.handler(session, args, ctx)
    except ToolError as exc:
        return ToolResult(name, "error", error=str(exc))
    except Exception:
        # Unexpected: log it for us, give the model nothing internal (no stack, no SQL).
        log.exception("tool.failed", tool=name)
        return ToolResult(name, "error", error="internal error")
    return ToolResult(name, "ok", data=data)


async def execute_tool(
    ctx: ToolContext,
    name: str,
    arguments: dict[str, Any],
    *,
    thread_id: uuid.UUID | None = None,
) -> ToolResult:
    result = await _run(ctx, name, arguments if isinstance(arguments, dict) else {})
    await record_audit(
        ctx.sessions,
        ctx.principal,
        "tool_call",
        outcome=result.status,
        tool_name=name,
        arguments=arguments,
        detail=result.error,
        thread_id=thread_id,
    )
    log.info("tool.call", tool=name, status=result.status)  # never the arguments or data
    return result
