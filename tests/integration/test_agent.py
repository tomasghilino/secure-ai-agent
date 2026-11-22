"""The agent graph end to end: real tools, real PostgreSQL (RLS, checkpoints), scripted model.

A scripted model replies with fixed messages, so each test controls exactly which tool
calls the "model" makes, including the malicious ones a manipulated model could make.
"""

from collections.abc import AsyncIterator, Callable
from datetime import date
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.checkpointer import open_checkpointer
from app.agents.models import ScriptedChatModel, call
from app.agents.service import AgentService, NotAllowed, ThreadBusy, ThreadNotFound
from app.core.config import Settings
from app.database.models import AuditEventRecord, ReturnAuthorizationRecord
from app.database.tenancy import tenant_session
from app.rag.embeddings import FakeEmbeddings
from app.tools.authorization import Principal
from scripts.seed_business_data import load_sources, seed

pytestmark = pytest.mark.integration

REFERENCE_DATE = date(2026, 11, 15)  # the date the seeded scenarios are written against
SUPPORT = Principal("nsi-amer", "agent-1", "support_agent")
SUPERVISOR = Principal("nsi-amer", "sup-1", "supervisor")
EMEA_SUPERVISOR = Principal("nsi-emea", "sup-9", "supervisor")

ServiceFactory = Callable[..., AgentService]


@pytest.fixture
async def seeded(
    sessions: async_sessionmaker[AsyncSession], session_with_returns_policies: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    await seed(session_with_returns_policies, load_sources())
    return sessions


@pytest.fixture
async def checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    async with open_checkpointer(database_url) as saver:
        yield saver


@pytest.fixture
def service(
    seeded: async_sessionmaker[AsyncSession], checkpointer: AsyncPostgresSaver
) -> ServiceFactory:
    def make(model: BaseChatModel, **settings: Any) -> AgentService:
        return AgentService(
            sessions=seeded,
            embeddings=FakeEmbeddings(),
            model=model,
            checkpointer=checkpointer,
            settings=Settings(  # type: ignore[call-arg]
                _env_file=None, **{"agent_reference_date": REFERENCE_DATE, **settings}
            ),
        )

    return make


def script(*replies: AIMessage) -> ScriptedChatModel:
    return ScriptedChatModel(script=list(replies))


def asks(*tool_calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(tool_calls))


async def audit(sessions: async_sessionmaker[AsyncSession]) -> list[tuple[str, str | None, str]]:
    async with sessions() as session:
        rows = await session.execute(
            select(
                AuditEventRecord.event_type, AuditEventRecord.tool_name, AuditEventRecord.outcome
            ).order_by(AuditEventRecord.occurred_at)
        )
        return [tuple(row) for row in rows]  # type: ignore[misc]


async def return_authorizations(
    sessions: async_sessionmaker[AsyncSession],
) -> list[ReturnAuthorizationRecord]:
    async with sessions() as session:
        return list(await session.scalars(select(ReturnAuthorizationRecord)))


# --------------------------------------------------------------------------- read-only runs


async def test_a_question_is_answered_from_tool_results(
    service: ServiceFactory, seeded: async_sessionmaker[AsyncSession]
) -> None:
    model = script(
        asks(call("get_order", order_number="SO-2026-0103")),
        AIMessage("SO-2026-0103 was delivered 40 days ago: outside the 30-day window."),
    )

    run = await service(model).start(SUPPORT, "Can C-1006 still return SO-2026-0103?")

    assert run.status == "completed"
    assert run.answer is not None and "40 days" in run.answer
    assert [(c.tool, c.status) for c in run.tool_calls] == [("get_order", "ok")]
    assert "get_invoice" not in model.offered[0]  # never offered to a support agent
    assert await audit(seeded) == [("tool_call", "get_order", "ok")]


async def test_a_tool_outside_the_role_is_denied_even_if_the_model_calls_it(
    service: ServiceFactory, seeded: async_sessionmaker[AsyncSession]
) -> None:
    model = script(
        asks(call("get_invoice", invoice_number="INV-2026-0106")),
        AIMessage("Your role cannot see invoices."),
    )

    run = await service(model).start(SUPPORT, "Is INV-2026-0106 overdue?")

    assert [(c.tool, c.status) for c in run.tool_calls] == [("get_invoice", "denied")]
    assert await audit(seeded) == [("tool_call", "get_invoice", "denied")]


async def test_another_tenants_records_are_invisible_to_the_tools(
    service: ServiceFactory,
) -> None:
    model = script(
        asks(call("get_order", order_number="SO-EU-2026-0201")),  # an nsi-emea order
        AIMessage("Not found."),
    )

    run = await service(model).start(SUPPORT, "Status of SO-EU-2026-0201?")

    assert [(c.tool, c.status) for c in run.tool_calls] == [("get_order", "error")]


async def test_the_loop_stops_at_the_step_limit(
    service: ServiceFactory, seeded: async_sessionmaker[AsyncSession]
) -> None:
    # Three distinct messages: LangGraph merges messages that share an id.
    model = script(*(asks(call("get_product", sku="CP-150S")) for _ in range(3)))

    run = await service(model, agent_max_steps=2).start(SUPPORT, "Tell me about CP-150S")

    assert run.status == "stopped"
    assert run.answer is not None and "stopped after 2 steps" in run.answer
    assert ("run_stopped", None, "step_limit") in await audit(seeded)


async def test_a_conversation_continues_only_for_its_owner_and_tenant(
    service: ServiceFactory,
) -> None:
    model = script(AIMessage("Hello."), AIMessage("Still here."))
    agent = service(model)
    run = await agent.start(SUPPORT, "Hi")

    second = await agent.send(SUPPORT, run.thread_id, "Are you there?")

    assert second.answer == "Still here."
    with pytest.raises(NotAllowed):
        await agent.send(Principal("nsi-amer", "agent-2", "support_agent"), run.thread_id, "x")
    with pytest.raises(ThreadNotFound):  # RLS: the other tenant cannot even see it
        await agent.view(EMEA_SUPERVISOR, run.thread_id)


# --------------------------------------------------------------------------- human approval


def proposes_return(order_number: str = "SO-2026-0101") -> AIMessage:
    return asks(
        call(
            "request_return_authorization",
            order_number=order_number,
            reason="Pump is oversized for the line; unused.",
        )
    )


async def test_a_return_waits_for_approval_and_then_executes(
    service: ServiceFactory, seeded: async_sessionmaker[AsyncSession]
) -> None:
    model = script(proposes_return(), AIMessage("Approved: RMA-SO-2026-0101."))
    agent = service(model)

    paused = await agent.start(SUPPORT, "Customer C-1001 wants to return SO-2026-0101")

    assert paused.status == "awaiting_approval"
    assert paused.pending_action is not None
    facts = paused.pending_action["facts"]  # read from the database for the approver
    assert (facts["order_number"], facts["days_since_delivery"]) == ("SO-2026-0101", 12)
    assert await return_authorizations(seeded) == []  # nothing written before approval
    assert [t.thread_id for t in await agent.pending_approvals(SUPERVISOR)] == [paused.thread_id]

    done = await agent.decide(SUPERVISOR, paused.thread_id, approved=True, comment="ok")

    assert done.status == "completed"
    [rma] = await return_authorizations(seeded)
    assert (rma.rma_number, rma.requested_by, rma.approved_by) == (
        "RMA-SO-2026-0101",
        "agent-1",
        "sup-1",
    )
    assert [event[0] for event in await audit(seeded)] == [
        "action_proposed",
        "action_decided",
        "action_executed",
    ]


async def test_a_rejected_action_writes_nothing(
    service: ServiceFactory, seeded: async_sessionmaker[AsyncSession]
) -> None:
    model = script(proposes_return(), AIMessage("The supervisor rejected the return."))
    agent = service(model)
    paused = await agent.start(SUPPORT, "Return SO-2026-0101")

    done = await agent.decide(SUPERVISOR, paused.thread_id, approved=False, comment="no")

    assert done.status == "completed"
    assert [(c.tool, c.status) for c in done.tool_calls] == [
        ("request_return_authorization", "rejected")
    ]
    assert await return_authorizations(seeded) == []


async def test_nobody_approves_their_own_request_and_only_supervisors_approve(
    service: ServiceFactory,
) -> None:
    agent = service(script(proposes_return(), proposes_return()))
    by_support = await agent.start(SUPPORT, "Return SO-2026-0101")
    by_supervisor = await agent.start(SUPERVISOR, "Return SO-2026-0101")

    with pytest.raises(NotAllowed, match="role support_agent"):
        await agent.decide(
            Principal("nsi-amer", "agent-2", "support_agent"),
            by_support.thread_id,
            approved=True,
            comment=None,
        )
    with pytest.raises(NotAllowed, match="requested it"):
        await agent.decide(SUPERVISOR, by_supervisor.thread_id, approved=True, comment=None)


async def test_a_decision_is_applied_once(service: ServiceFactory) -> None:
    agent = service(script(proposes_return(), AIMessage("Done.")))
    paused = await agent.start(SUPPORT, "Return SO-2026-0101")
    await agent.decide(SUPERVISOR, paused.thread_id, approved=True, comment=None)

    with pytest.raises(ThreadBusy):
        await agent.decide(SUPERVISOR, paused.thread_id, approved=True, comment=None)


async def test_an_injected_return_never_reaches_a_supervisor(
    service: ServiceFactory, seeded: async_sessionmaker[AsyncSession]
) -> None:
    # Worst case: ticket T-5009 tells the model to request a return, and the model obeys.
    # SO-2026-0102 is a build-to-order pump: the rules in code refuse the proposal, so not
    # even a careless approver could execute it (found by the red-team suite).
    model = script(
        asks(call("get_ticket", ticket_number="T-5009")),
        proposes_return("SO-2026-0102"),
        AIMessage("I cannot request a return for this order."),
    )

    run = await service(model).start(SUPPORT, "Summarise ticket T-5009")

    assert run.status == "completed"
    assert run.pending_action is None
    assert ("request_return_authorization", "error") in [(c.tool, c.status) for c in run.tool_calls]
    assert ("action_proposed", "request_return_authorization", "error") in await audit(seeded)
    assert await return_authorizations(seeded) == []


@pytest.mark.parametrize(
    ("order_number", "reason"),
    [
        ("SO-2026-0103", "outside the 30-day return window"),  # delivered 40 days ago
        ("SO-2026-0107", "not returnable"),  # one line was sold as final sale
    ],
)
async def test_returns_outside_the_rules_are_refused_in_code(
    service: ServiceFactory,
    seeded: async_sessionmaker[AsyncSession],
    order_number: str,
    reason: str,
) -> None:
    model = script(proposes_return(order_number), AIMessage("Not possible."))

    run = await service(model).start(SUPPORT, f"Create a return authorization for {order_number}")

    assert run.status == "completed"
    assert run.pending_action is None
    async with seeded() as session:
        detail = await session.scalar(
            select(AuditEventRecord.detail).where(AuditEventRecord.event_type == "action_proposed")
        )
    assert detail is not None and reason in detail


async def test_the_rules_are_checked_again_when_the_supervisor_decides(
    service: ServiceFactory, seeded: async_sessionmaker[AsyncSession]
) -> None:
    paused = await service(script(proposes_return())).start(SUPPORT, "Return SO-2026-0101")
    assert paused.status == "awaiting_approval"  # 12 days after delivery: eligible

    # The approval arrives late: 47 days after delivery, outside the window.
    late = service(
        script(AIMessage("The window has passed.")), agent_reference_date=date(2026, 12, 20)
    )
    done = await late.decide(SUPERVISOR, paused.thread_id, approved=True, comment="ok")

    assert done.status == "completed"
    assert await return_authorizations(seeded) == []
    assert ("action_executed", "request_return_authorization", "error") in await audit(seeded)


# --------------------------------------------------------------------------- database controls


async def test_the_audit_log_is_append_only_for_the_application(
    service: ServiceFactory, seeded: async_sessionmaker[AsyncSession]
) -> None:
    await service(script(AIMessage("Hi."))).start(SUPPORT, "Hi")
    await service(script(asks(call("get_product", sku="CP-150S")), AIMessage("ok"))).start(
        SUPPORT, "CP-150S?"
    )

    for statement in ("UPDATE audit_events SET outcome = 'ok'", "DELETE FROM audit_events"):
        async with tenant_session(seeded, "nsi-amer") as session:
            with pytest.raises(ProgrammingError, match="permission denied"):
                async with session.begin():
                    await session.execute(text(statement))


async def test_the_database_rejects_a_self_approved_return(
    seeded: async_sessionmaker[AsyncSession],
) -> None:
    async with seeded() as session:
        with pytest.raises(IntegrityError, match="approver_is_not_requester"):
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO return_authorizations (tenant_id, rma_number, order_id,"
                        " reason, requested_by, approved_by) SELECT tenant_id, 'RMA-X', id,"
                        " 'x', 'sup-1', 'sup-1' FROM orders WHERE order_number = 'SO-2026-0101'"
                    )
                )
