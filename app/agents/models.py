"""Chat models for the agent: real ones from the RAG factory, plus two offline fakes.

- ``FakeAgentChatModel`` (``LLM_PROVIDER=fake``): rule-based and deterministic. It looks
  up the identifiers it finds in the question (SO-…, T-…, INV-…) with the matching tool,
  or searches the policies, then summarises the tool results. Enough to exercise the API
  and the graph in CI without a model.
- ``ScriptedChatModel`` (tests): replies with a fixed list of messages and records which
  tools it was offered, to test the graph step by step.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import Field

from app.core.config import Settings
from app.rag.llm import build_chat_model
from app.tools.authorization import REQUEST_RETURN

ToolLike = dict[str, Any] | type | Callable[..., Any] | BaseTool

_IDENTIFIERS = (
    (re.compile(r"\bSO-(?:EU-)?\d{4}-\d{4}\b"), "get_order", "order_number"),
    (re.compile(r"\bT-\d{4}\b"), "get_ticket", "ticket_number"),
    (re.compile(r"\bINV-(?:EU-)?\d{4}-\d{4}\b"), "get_invoice", "invoice_number"),
    (re.compile(r"\bC-\d{4}\b"), "get_customer", "customer_number"),
    (re.compile(r"\bSUP-[A-Z0-9]{3,5}\b"), "get_supplier", "supplier_code"),
)


def _tool_names(tools: Sequence[ToolLike]) -> list[str]:
    return [t["function"]["name"] for t in tools if isinstance(t, dict)]


def _since_last_user_message(messages: list[BaseMessage]) -> tuple[str, list[ToolMessage]]:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            rest = messages[index + 1 :]
            return str(messages[index].content), [m for m in rest if isinstance(m, ToolMessage)]
    return "", []


class FakeAgentChatModel(BaseChatModel):
    offered: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "fake-agent"

    def bind_tools(
        self, tools: Sequence[ToolLike], *, tool_choice: str | None = None, **kwargs: Any
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self.model_copy(update={"offered": _tool_names(tools)})

    def _plan(self, question: str) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for pattern, tool, argument in _IDENTIFIERS:
            if tool not in self.offered:
                continue
            calls += [{"name": tool, "args": {argument: m}} for m in pattern.findall(question)]
        orders = _IDENTIFIERS[0][0].findall(question)
        wants_return = "return authorization" in question.lower() or "rma" in question.lower()
        if wants_return and orders and REQUEST_RETURN in self.offered:
            reason = question[:200]
            calls.append(
                {"name": REQUEST_RETURN, "args": {"order_number": orders[0], "reason": reason}}
            )
        if not calls and "search_policies" in self.offered:
            calls.append({"name": "search_policies", "args": {"query": question[:300]}})
        return [{**call, "id": f"call_{i}", "type": "tool_call"} for i, call in enumerate(calls)]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        question, results = _since_last_user_message(messages)
        if results:
            lines = [f"- {m.name}: {str(m.content)[:300]}" for m in results]
            message = AIMessage(content="Here is what I found:\n" + "\n".join(lines))
        else:
            calls = self._plan(question)
            message = (
                AIMessage(content="", tool_calls=calls)
                if calls
                else AIMessage(content="I cannot access the information needed for this.")
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


class ScriptedChatModel(BaseChatModel):
    script: list[AIMessage]
    offered: list[list[str]] = Field(default_factory=list)  # tool names, per model call

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(
        self, tools: Sequence[ToolLike], *, tool_choice: str | None = None, **kwargs: Any
    ) -> Runnable[LanguageModelInput, AIMessage]:
        self.offered.append(_tool_names(tools))
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not self.script:
            raise AssertionError("the scripted model has no replies left")
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def call(name: str, call_id: str = "call_1", **args: Any) -> dict[str, Any]:
    """A tool call as a model would emit it (for scripts and tests)."""
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def build_agent_model(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "fake":
        return FakeAgentChatModel()
    # Ollama Qwen3 / Bedrock: both support tool calling.
    return build_chat_model(settings.model_copy(update={"llm_reasoning": settings.agent_reasoning}))
