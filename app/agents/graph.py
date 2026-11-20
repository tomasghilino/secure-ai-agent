"""The operations agent as an explicit LangGraph state machine.

    START → agent ──(tool calls)──→ tools ──(action proposed)──→ approval ─┐
              ▲  └─(no tool calls)→ END   └──────(reads only)───┐          │
              └─────────────────────────────────────────────────┴──────────┘
            agent ──(step limit reached)──→ step_limit → END

- ``agent``: one model call with the tools the user's role allows.
- ``tools``: runs read-only tools through the executor (authorized, validated, audited);
  a call to ``request_return_authorization`` only creates a *pending action*.
- ``approval``: ``interrupt()`` pauses the run and saves its state in PostgreSQL (the
  checkpointer). A supervisor's decision resumes it, possibly hours later, in another
  process. The node re-runs from its start on resume, so nothing before ``interrupt``
  may have side effects.
- ``step_limit``: the loop is bounded; the run stops instead of spinning (OWASP LLM10).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from app.agents.executors import ToolExecutor
from app.agents.prompts import system_prompt
from app.core.config import Settings
from app.tools.actions import REQUEST_RETURN_SPEC, ApprovalDecision, decide, propose
from app.tools.audit import record_audit
from app.tools.authorization import REQUEST_RETURN, is_allowed
from app.tools.base import ToolContext, to_json


class AgentState(TypedDict, total=False):
    # add_messages appends (and replaces by id) instead of overwriting the list.
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int  # model calls since the last user message
    pending_action: dict[str, Any] | None
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class AgentLimits:
    max_steps: int = 6
    max_tool_calls_per_step: int = 4
    tool_result_max_chars: int = 4000

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentLimits:
        return cls(
            settings.agent_max_steps,
            settings.agent_max_tool_calls_per_step,
            settings.agent_tool_result_max_chars,
        )


@dataclass(frozen=True, slots=True)
class AgentDeps:
    model: BaseChatModel
    tools: ToolExecutor
    # The requester's identity and resources: the agent always acts as the requester.
    context: ToolContext
    limits: AgentLimits
    thread_id: uuid.UUID


def _reply(call_id: str, name: str, body: dict[str, Any], max_chars: int) -> ToolMessage:
    # JSON-encoded and size-capped: tool output is untrusted data, kept clearly delimited
    # and small (a huge result costs tokens and buries the useful part).
    content = to_json(body)
    if len(content) > max_chars:
        content = content[:max_chars] + " …[truncated]"
    return ToolMessage(content=content, tool_call_id=call_id, name=name)


def build_agent_graph(deps: AgentDeps) -> StateGraph[AgentState]:
    limits = deps.limits
    role = deps.context.principal.role

    def offered_tools() -> list[Any]:
        specs = deps.tools.specs()
        if is_allowed(role, REQUEST_RETURN):
            specs = [*specs, REQUEST_RETURN_SPEC]
        return specs

    async def agent(state: AgentState) -> dict[str, Any]:
        specs = offered_tools()
        model = deps.model.bind_tools([spec.as_model_tool() for spec in specs])
        prompt = system_prompt(deps.context, [spec.name for spec in specs])
        response = await model.ainvoke([SystemMessage(prompt), *state["messages"]])
        return {"messages": [response], "steps": state.get("steps", 0) + 1}

    async def tools(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)  # noqa: S101 (routing guarantees it)
        replies: list[ToolMessage] = []
        pending: dict[str, Any] | None = None
        reads = 0
        for index, call in enumerate(last.tool_calls):
            call_id = call.get("id") or f"call_{index}"
            name, args = call["name"], call.get("args") or {}
            if name == REQUEST_RETURN:
                if pending is not None:
                    body = {"status": "error", "error": "only one action per turn"}
                    replies.append(_reply(call_id, name, body, limits.tool_result_max_chars))
                    continue
                outcome = await propose(deps.context, call_id, args, thread_id=deps.thread_id)
                if outcome["status"] == "pending_approval":
                    pending = outcome  # its reply is written after the decision
                else:
                    replies.append(_reply(call_id, name, outcome, limits.tool_result_max_chars))
                continue
            if reads >= limits.max_tool_calls_per_step:
                body = {"tool": name, "status": "error", "error": "too many tool calls in one turn"}
                replies.append(_reply(call_id, name, body, limits.tool_result_max_chars))
                continue
            reads += 1
            result = await deps.tools.call(name, args)
            replies.append(_reply(call_id, name, result.as_dict(), limits.tool_result_max_chars))
        return {"messages": replies, "pending_action": pending}

    async def approval(state: AgentState) -> dict[str, Any]:
        pending = state["pending_action"]
        assert pending is not None  # noqa: S101 (routing guarantees it)
        # Pauses here the first time; on resume, interrupt() returns the decision.
        decision = ApprovalDecision.model_validate(
            interrupt({"type": "approval_required", "action": pending})
        )
        result = await decide(deps.context, pending, decision, thread_id=deps.thread_id)
        reply = _reply(
            pending["tool_call_id"], REQUEST_RETURN, result, limits.tool_result_max_chars
        )
        return {"messages": [reply], "pending_action": None}

    async def step_limit(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)  # noqa: S101
        skipped = {"status": "error", "error": "not executed: step limit reached"}
        replies = [
            _reply(
                call.get("id") or f"call_{i}", call["name"], skipped, limits.tool_result_max_chars
            )
            for i, call in enumerate(last.tool_calls)
        ]
        note = AIMessage(
            content=f"I stopped after {limits.max_steps} steps without a final answer. "
            "Please ask a narrower question."
        )
        await record_audit(
            deps.context.sessions,
            deps.context.principal,
            "run_stopped",
            outcome="step_limit",
            detail=f"{limits.max_steps} model calls",
            thread_id=deps.thread_id,
        )
        return {"messages": [*replies, note], "stop_reason": "step_limit"}

    def after_agent(state: AgentState) -> Literal["tools", "step_limit", "__end__"]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "step_limit" if state.get("steps", 0) >= limits.max_steps else "tools"
        return "__end__"  # = END

    def after_tools(state: AgentState) -> Literal["approval", "agent"]:
        return "approval" if state.get("pending_action") else "agent"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_node("approval", approval)
    graph.add_node("step_limit", step_limit)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent)
    graph.add_conditional_edges("tools", after_tools)
    graph.add_edge("approval", "agent")
    graph.add_edge("step_limit", END)
    return graph
