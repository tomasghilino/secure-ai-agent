"""Agent threads: start, continue, approve; the layer between the API and the graph.

Responsibilities that do not belong in the graph:

- **Ownership**: a thread row in ``agent_threads`` (RLS) is checked before its checkpoint
  is touched. Other tenants get "not found"; only the owner continues a conversation.
- **Approval rules**: only an approver role, never the requester (separation of duties).
- **Concurrency**: a thread is *claimed* with an atomic ``UPDATE … WHERE status = …``, so
  two simultaneous approvals cannot both resume the run (the same pattern as the worker).
- **Identity**: after approval, the run resumes as the *requester*; the approver's
  identity only travels inside the decision.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.executors import LocalToolExecutor, ToolExecutor
from app.agents.graph import AgentDeps, AgentLimits, build_agent_graph
from app.agents.mcp_client import McpToolExecutor, stdio_server_parameters
from app.core.config import Settings
from app.database.models import AgentThreadRecord, AuditEventRecord
from app.database.tenancy import tenant_session
from app.rag.embeddings import EmbeddingProvider
from app.security.limits import TokenCounter
from app.tools.actions import ApprovalDecision
from app.tools.authorization import (
    APPROVER_ROLES,
    Principal,
    Role,
    approval_denied_reason,
    parse_role,
)
from app.tools.base import ToolContext

ThreadStatus = Literal["running", "awaiting_approval", "completed", "stopped", "failed"]
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)


class AgentError(Exception):
    status_code = 400


class ThreadNotFound(AgentError):
    status_code = 404


class NotAllowed(AgentError):
    status_code = 403


class ThreadBusy(AgentError):
    status_code = 409


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    tool: str
    arguments: dict[str, Any]
    status: str


@dataclass(frozen=True, slots=True)
class AgentRun:
    thread_id: uuid.UUID
    status: ThreadStatus
    answer: str | None
    pending_action: dict[str, Any] | None
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    steps: int = 0
    tokens: int = 0  # model tokens this request consumed (input + output)


@dataclass(frozen=True, slots=True)
class ThreadView:
    thread_id: uuid.UUID
    status: str
    title: str
    created_by: str
    created_by_role: str
    pending_action: dict[str, Any] | None
    answer: str | None
    created_at: datetime
    updated_at: datetime


def _since_last_user_message(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return list(messages[index + 1 :])
    return list(messages)


def final_answer(messages: Sequence[BaseMessage]) -> str | None:
    """The last model message without tool calls, after the last user message."""
    for message in reversed(_since_last_user_message(messages)):
        if isinstance(message, AIMessage) and not message.tool_calls and message.content:
            return _THINKING.sub("", message.text).strip() or None
    return None


def tool_trace(messages: Sequence[BaseMessage]) -> list[ToolCallTrace]:
    recent = _since_last_user_message(messages)
    statuses: dict[str, str] = {}
    for message in recent:
        if isinstance(message, ToolMessage):
            try:
                statuses[message.tool_call_id] = str(json.loads(message.text)["status"])
            except (ValueError, KeyError, TypeError):
                statuses[message.tool_call_id] = "unknown"
    return [
        ToolCallTrace(
            call["name"], call.get("args") or {}, statuses.get(call["id"] or "", "pending")
        )
        for message in recent
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]


class AgentService:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        embeddings: EmbeddingProvider,
        model: BaseChatModel,
        checkpointer: BaseCheckpointSaver[Any],
        settings: Settings,
    ) -> None:
        self._sessions = sessions
        self._embeddings = embeddings
        self._model = model
        self._checkpointer = checkpointer
        self._settings = settings
        self._limits = AgentLimits.from_settings(settings)

    # ------------------------------------------------------------------ public API

    async def start(self, principal: Principal, message: str) -> AgentRun:
        thread_id = uuid.uuid4()
        async with self._tenant(principal.tenant_id) as session, session.begin():
            session.add(
                AgentThreadRecord(
                    id=thread_id,
                    tenant_id=principal.tenant_id,
                    created_by=principal.user_id,
                    created_by_role=principal.role,
                    title=message[:200],
                    status="running",
                )
            )
        return await self._run(principal, thread_id, self._new_turn(message))

    async def send(self, principal: Principal, thread_id: uuid.UUID, message: str) -> AgentRun:
        thread = await self._load(principal, thread_id)
        if thread.created_by != principal.user_id:
            raise NotAllowed("only the user who started the conversation can continue it")
        await self._claim(principal.tenant_id, thread_id, ("completed", "stopped", "failed"))
        return await self._run(principal, thread_id, self._new_turn(message))

    async def decide(
        self, approver: Principal, thread_id: uuid.UUID, *, approved: bool, comment: str | None
    ) -> AgentRun:
        thread = await self._load(approver, thread_id)
        reason = approval_denied_reason(approver, thread.created_by)
        if reason is not None:
            raise NotAllowed(reason)
        await self._claim(approver.tenant_id, thread_id, ("awaiting_approval",))
        owner = Principal(thread.tenant_id, thread.created_by, _role(thread.created_by_role))
        decision = ApprovalDecision(
            approved=approved,
            approver_id=approver.user_id,
            approver_role=approver.role,
            comment=comment,
        )
        return await self._run(owner, thread_id, Command(resume=decision.model_dump()))

    async def view(self, principal: Principal, thread_id: uuid.UUID) -> ThreadView:
        thread = await self._load(principal, thread_id)
        if thread.created_by != principal.user_id and principal.role not in APPROVER_ROLES:
            raise ThreadNotFound("thread not found")
        checkpoint = await self._checkpointer.aget_tuple(self._config(thread_id))
        messages = checkpoint.checkpoint["channel_values"].get("messages", []) if checkpoint else []
        return _view(thread, final_answer(messages))

    async def pending_approvals(self, approver: Principal) -> list[ThreadView]:
        if approver.role not in APPROVER_ROLES:
            raise NotAllowed(f"role {approver.role} cannot approve actions")
        async with self._tenant(approver.tenant_id) as session:
            threads = await session.scalars(
                select(AgentThreadRecord)
                .where(
                    AgentThreadRecord.tenant_id == approver.tenant_id,
                    AgentThreadRecord.status == "awaiting_approval",
                )
                .order_by(AgentThreadRecord.updated_at)
                .limit(50)
            )
            return [_view(thread, None) for thread in threads]

    async def audit_events(
        self, principal: Principal, *, thread_id: uuid.UUID | None, limit: int
    ) -> list[AuditEventRecord]:
        if principal.role not in APPROVER_ROLES:
            raise NotAllowed(f"role {principal.role} cannot read the audit log")
        query = select(AuditEventRecord).where(AuditEventRecord.tenant_id == principal.tenant_id)
        if thread_id is not None:
            query = query.where(AuditEventRecord.thread_id == thread_id)
        async with self._tenant(principal.tenant_id) as session:
            events = await session.scalars(
                query.order_by(AuditEventRecord.occurred_at.desc()).limit(limit)
            )
            return list(events)

    # ------------------------------------------------------------------ internals

    def _tenant(self, tenant_id: str) -> AsyncSession:
        return tenant_session(self._sessions, tenant_id)

    @staticmethod
    def _config(thread_id: uuid.UUID) -> RunnableConfig:
        return {"configurable": {"thread_id": str(thread_id)}}

    @staticmethod
    def _new_turn(message: str) -> dict[str, Any]:
        # A new user message resets the step budget and any stop reason.
        return {"messages": [HumanMessage(message)], "steps": 0, "stop_reason": None}

    def _context(self, principal: Principal) -> ToolContext:
        return ToolContext(
            sessions=self._sessions,
            embeddings=self._embeddings,
            principal=principal,
            today=self._settings.agent_reference_date or date.today(),
            retrieval_mode=self._settings.rag_retrieval_mode,
        )

    @asynccontextmanager
    async def _executor(
        self, ctx: ToolContext, thread_id: uuid.UUID
    ) -> AsyncIterator[ToolExecutor]:
        if self._settings.agent_tool_transport == "mcp":
            params = stdio_server_parameters(ctx.principal, self._settings)
            async with McpToolExecutor(params, ctx.principal) as executor:
                yield executor
        else:
            yield LocalToolExecutor(ctx, thread_id)

    async def _load(self, principal: Principal, thread_id: uuid.UUID) -> AgentThreadRecord:
        async with self._tenant(principal.tenant_id) as session:
            thread = await session.get(AgentThreadRecord, thread_id)  # RLS: own tenant only
        if thread is None:
            raise ThreadNotFound("thread not found")
        return thread

    async def _claim(
        self, tenant_id: str, thread_id: uuid.UUID, expected: tuple[ThreadStatus, ...]
    ) -> None:
        async with self._tenant(tenant_id) as session, session.begin():
            claimed = await session.scalar(
                update(AgentThreadRecord)
                .where(AgentThreadRecord.id == thread_id, AgentThreadRecord.status.in_(expected))
                .values(status="running")
                .returning(AgentThreadRecord.id)
            )
        if claimed is None:
            raise ThreadBusy(f"thread is not in state {' or '.join(expected)}")

    async def _set_status(
        self, tenant_id: str, thread_id: uuid.UUID, status: ThreadStatus, pending: Any
    ) -> None:
        async with self._tenant(tenant_id) as session, session.begin():
            await session.execute(
                update(AgentThreadRecord)
                .where(AgentThreadRecord.id == thread_id)
                .values(status=status, pending_action=pending)
            )

    async def _run(self, owner: Principal, thread_id: uuid.UUID, graph_input: Any) -> AgentRun:
        ctx = self._context(owner)
        config = self._config(thread_id)
        # Each step is two graph nodes (agent, tools); leave room for approval.
        config["recursion_limit"] = 4 * self._limits.max_steps + 10
        # Sees every model call of this run, for the tenant's token budget.
        tokens = TokenCounter()
        config["callbacks"] = [tokens]
        try:
            async with self._executor(ctx, thread_id) as executor:
                deps = AgentDeps(self._model, executor, ctx, self._limits, thread_id)
                graph = build_agent_graph(deps).compile(checkpointer=self._checkpointer)
                await graph.ainvoke(graph_input, config)
                snapshot = await graph.aget_state(config)
        except Exception:
            await self._set_status(owner.tenant_id, thread_id, "failed", None)
            raise

        values = snapshot.values
        messages: list[BaseMessage] = values.get("messages", [])
        status: ThreadStatus
        if snapshot.interrupts:
            status, pending = "awaiting_approval", values.get("pending_action")
        else:
            status, pending = ("stopped" if values.get("stop_reason") else "completed"), None
        await self._set_status(owner.tenant_id, thread_id, status, pending)
        return AgentRun(
            thread_id=thread_id,
            status=status,
            answer=None if pending else final_answer(messages),
            pending_action=pending,
            tool_calls=tool_trace(messages),
            steps=int(values.get("steps", 0)),
            tokens=tokens.total,
        )


def _role(value: str) -> Role:
    role = parse_role(value)
    if role is None:
        raise AgentError(f"unknown role {value}")
    return role


def _view(thread: AgentThreadRecord, answer: str | None) -> ThreadView:
    return ThreadView(
        thread_id=thread.id,
        status=thread.status,
        title=thread.title,
        created_by=thread.created_by,
        created_by_role=thread.created_by_role,
        pending_action=thread.pending_action,
        answer=answer,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )
