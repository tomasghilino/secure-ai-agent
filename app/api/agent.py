"""Agent endpoints: conversations, human approval and the audit log."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.agents.service import AgentError, AgentRun, AgentService, ThreadView
from app.api.dependencies import PrincipalDep, SettingsDep

router = APIRouter(prefix="/v1", tags=["agent"])


def get_agent_service(request: Request, settings: SettingsDep) -> AgentService:
    if not settings.agent_enabled:  # kill switch
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the agent is disabled")
    service: AgentService = request.app.state.agent_service
    return service


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except AgentError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=2000)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approve: bool
    comment: str | None = Field(default=None, max_length=500)


class ToolCallModel(BaseModel):
    tool: str
    arguments: dict[str, Any]
    status: str


class RunResponse(BaseModel):
    thread_id: uuid.UUID
    status: str
    answer: str | None
    pending_action: dict[str, Any] | None = Field(
        description="The action waiting for a supervisor, with facts from the database"
    )
    tool_calls: list[ToolCallModel]
    steps: int

    @classmethod
    def of(cls, run: AgentRun) -> RunResponse:
        return cls(
            thread_id=run.thread_id,
            status=run.status,
            answer=run.answer,
            pending_action=run.pending_action,
            tool_calls=[ToolCallModel(**vars_of(c)) for c in run.tool_calls],
            steps=run.steps,
        )


class ThreadResponse(BaseModel):
    thread_id: uuid.UUID
    status: str
    title: str
    created_by: str
    created_by_role: str
    pending_action: dict[str, Any] | None
    answer: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: ThreadView) -> ThreadResponse:
        return cls(**vars_of(view))


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    occurred_at: datetime
    actor_id: str
    actor_role: str
    thread_id: uuid.UUID | None
    event_type: str
    tool_name: str | None
    arguments: dict[str, Any] | None
    outcome: str
    detail: str | None


def vars_of(obj: Any) -> dict[str, Any]:
    return {name: getattr(obj, name) for name in obj.__slots__}


@router.post("/agent/threads")
async def start_thread(
    body: MessageRequest, principal: PrincipalDep, agent: AgentServiceDep
) -> RunResponse:
    """Start a conversation with the operations agent."""
    return RunResponse.of(await _call(agent.start(principal, body.message)))


@router.post("/agent/threads/{thread_id}/messages")
async def send_message(
    thread_id: uuid.UUID, body: MessageRequest, principal: PrincipalDep, agent: AgentServiceDep
) -> RunResponse:
    """Continue a conversation (its owner only)."""
    return RunResponse.of(await _call(agent.send(principal, thread_id, body.message)))


@router.post("/agent/threads/{thread_id}/decision")
async def decide(
    thread_id: uuid.UUID, body: DecisionRequest, principal: PrincipalDep, agent: AgentServiceDep
) -> RunResponse:
    """Approve or reject the pending action (supervisors, never the requester)."""
    run = agent.decide(principal, thread_id, approved=body.approve, comment=body.comment)
    return RunResponse.of(await _call(run))


@router.get("/agent/threads/{thread_id}")
async def get_thread(
    thread_id: uuid.UUID, principal: PrincipalDep, agent: AgentServiceDep
) -> ThreadResponse:
    return ThreadResponse.of(await _call(agent.view(principal, thread_id)))


@router.get("/agent/approvals")
async def pending_approvals(
    principal: PrincipalDep, agent: AgentServiceDep
) -> list[ThreadResponse]:
    """Threads waiting for a decision in the caller's tenant (supervisors)."""
    return [ThreadResponse.of(view) for view in await _call(agent.pending_approvals(principal))]


@router.get("/audit-events")
async def audit_events(
    principal: PrincipalDep,
    agent: AgentServiceDep,
    thread_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditEventResponse]:
    """Most recent audit events of the caller's tenant (supervisors)."""
    events = await _call(agent.audit_events(principal, thread_id=thread_id, limit=limit))
    return [AuditEventResponse.model_validate(event) for event in events]
