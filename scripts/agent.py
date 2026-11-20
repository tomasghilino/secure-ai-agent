"""Talk to the operations agent from the terminal (``make agent`` / ``make approve``).

    make agent q="Can C-1006 return SO-2026-0103?"                 # as a support agent
    make agent q="Which invoices are overdue?" role=finance_analyst user=fin-1
    make approve t=<thread_id>            # as supervisor sup-1; add reject=1 to reject

Uses the providers in .env (Ollama locally) and the development database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from app.agents.checkpointer import open_checkpointer
from app.agents.models import build_agent_model
from app.agents.service import AgentRun, AgentService
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.session import build_engine, build_session_factory
from app.rag.embeddings import build_embedding_provider
from app.tools.authorization import ROLES, Principal


def show(run: AgentRun) -> None:
    print(f"\nthread {run.thread_id}  status={run.status}  steps={run.steps}")
    for call in run.tool_calls:
        print(f"  tool {call.tool}({json.dumps(call.arguments)}) -> {call.status}")
    if run.pending_action:
        facts = run.pending_action["facts"]
        print(
            "\nWAITING FOR APPROVAL:", run.pending_action["tool"], run.pending_action["arguments"]
        )
        print("  facts from the database:", json.dumps(facts, indent=2)[:1500])
        print(f"\n  approve: make approve t={run.thread_id}   reject: add reject=1")
    if run.answer:
        print("\n" + run.answer)


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging("WARNING", "console")
    engine = build_engine(settings.database_url)
    principal = Principal(args.tenant, args.user, args.role)
    try:
        async with open_checkpointer(settings.database_url) as checkpointer:
            agent = AgentService(
                sessions=build_session_factory(engine),
                embeddings=build_embedding_provider(settings),
                model=build_agent_model(settings),
                checkpointer=checkpointer,
                settings=settings,
            )
            if args.decide:
                run = await agent.decide(
                    principal,
                    uuid.UUID(args.decide),
                    approved=not args.reject,
                    comment=args.comment,
                )
            elif args.thread:
                run = await agent.send(principal, uuid.UUID(args.thread), args.question)
            else:
                run = await agent.start(principal, args.question)
            show(run)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="?", default="")
    parser.add_argument("--tenant", default="nsi-amer")
    parser.add_argument("--user", default="agent-1")
    parser.add_argument("--role", default="support_agent", choices=ROLES)
    parser.add_argument("--thread", help="continue this conversation")
    parser.add_argument("--decide", metavar="THREAD_ID", help="approve or reject its action")
    parser.add_argument("--reject", action="store_true")
    parser.add_argument("--comment")
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(parser.parse_args()), loop_factory=loop_factory)
