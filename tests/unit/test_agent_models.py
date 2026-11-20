"""Offline agent models and how a run's messages become an API response."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.models import FakeAgentChatModel, call
from app.agents.service import final_answer, tool_trace
from app.tools.catalog import read_tools_for

SUPPORT_TOOLS = [spec.as_model_tool() for spec in read_tools_for("support_agent")]


def test_the_fake_model_looks_up_identifiers_it_may_use() -> None:
    model = FakeAgentChatModel().bind_tools(SUPPORT_TOOLS)

    reply = model.invoke([HumanMessage("Status of SO-2026-0101 and invoice INV-2026-0101?")])

    # get_invoice is not offered to a support agent, so it is not called.
    assert [c["name"] for c in reply.tool_calls] == ["get_order"]


def test_the_fake_model_searches_the_policies_without_identifiers() -> None:
    model = FakeAgentChatModel().bind_tools(SUPPORT_TOOLS)

    reply = model.invoke([HumanMessage("How long is the return window?")])

    assert reply.tool_calls[0]["name"] == "search_policies"


def test_the_fake_model_summarises_tool_results() -> None:
    model = FakeAgentChatModel().bind_tools(SUPPORT_TOOLS)
    history = [
        SystemMessage("rules"),
        HumanMessage("SO-2026-0101?"),
        AIMessage("", tool_calls=[call("get_order", order_number="SO-2026-0101")]),
        ToolMessage('{"status": "ok"}', tool_call_id="call_1", name="get_order"),
    ]

    reply = model.invoke(history)

    assert not reply.tool_calls
    assert "get_order" in reply.text


def test_the_answer_and_trace_cover_the_last_turn_only() -> None:
    messages = [
        HumanMessage("first question"),
        AIMessage("first answer"),
        HumanMessage("second question"),
        AIMessage("", tool_calls=[call("get_order", order_number="SO-2026-0101")]),
        ToolMessage('{"status": "denied"}', tool_call_id="call_1", name="get_order"),
        AIMessage("<think>hidden reasoning</think>Second answer."),
    ]

    assert final_answer(messages) == "Second answer."
    [trace] = tool_trace(messages)
    assert (trace.tool, trace.status) == ("get_order", "denied")
