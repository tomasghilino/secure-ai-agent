"""Append-only audit trail of tool calls and approvals (OWASP ASI10, STRIDE repudiation).

Each event is written in its own short transaction, so it is kept even when the tool
call or the run that produced it fails afterwards.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import AuditEventRecord
from app.database.tenancy import tenant_session
from app.tools.authorization import Principal

EventType = Literal[
    "tool_call", "action_proposed", "action_decided", "action_executed", "run_stopped"
]
MAX_ARGUMENT_CHARS = 300
MAX_ARGUMENTS = 20


def redact(arguments: Any) -> dict[str, Any] | None:
    """Arguments come from the model: keep them small and JSON-shaped before storing."""
    if arguments is None:
        return None
    if not isinstance(arguments, dict):
        return {"_raw": str(arguments)[:MAX_ARGUMENT_CHARS]}
    kept: dict[str, Any] = {}
    for key, value in list(arguments.items())[:MAX_ARGUMENTS]:
        if isinstance(value, bool | int | float) or value is None:
            kept[str(key)[:64]] = value
        else:
            kept[str(key)[:64]] = str(value)[:MAX_ARGUMENT_CHARS]
    return kept


async def record_audit(
    sessions: async_sessionmaker[AsyncSession],
    actor: Principal,
    event_type: EventType,
    *,
    outcome: str,
    tool_name: str | None = None,
    arguments: Any = None,
    detail: str | None = None,
    thread_id: uuid.UUID | None = None,
) -> None:
    async with tenant_session(sessions, actor.tenant_id) as session, session.begin():
        session.add(
            AuditEventRecord(
                tenant_id=actor.tenant_id,
                actor_id=actor.user_id,
                actor_role=actor.role,
                thread_id=thread_id,
                event_type=event_type,
                tool_name=tool_name,
                arguments=redact(arguments),
                outcome=outcome,
                detail=detail[:500] if detail else None,
            )
        )
