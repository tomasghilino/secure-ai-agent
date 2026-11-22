"""Run the AI red-team cases (data/evaluation/red_team.yaml) against the operations agent.

Uses the providers in .env (LLM_PROVIDER=ollama for the real local model, free) and the
development database (needs `make seed ingest`). Usage:
    uv run python -m scripts.evaluate_security [--cases DPI-01,IPI-01]
Prints the verdict per case and the attack success rate; details go to reports/.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.agents.checkpointer import open_checkpointer
from app.agents.models import build_agent_model
from app.agents.service import AgentService
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.session import build_engine, build_session_factory
from app.evaluation.red_team import AttackResult, attack_success_rate, load_cases, run_red_team
from app.rag.embeddings import build_embedding_provider

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


def summary(results: list[AttackResult]) -> None:
    outcomes = Counter(r.outcome for r in results)
    gated = [r for r in results if r.careless_outcome is not None]
    total = len(results)
    print("\n| Verdict | Cases |")
    print("| --- | --- |")
    for outcome in ("resisted", "contained", "succeeded", "error"):
        print(f"| {outcome} | {outcomes[outcome]}/{total} |")
    print(f"\nAttack success rate: {attack_success_rate(results):.0%}")
    if gated:
        executed = sum(r.careless_outcome == "succeeded" for r in gated)
        print(
            f"Reached the approval gate: {len(gated)}; executed with a careless approver: "
            f"{executed}/{len(gated)}"
        )
    print("\n| Case | Category | Verdict | Careless approver | Controls that fired | Tools |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in results:
        print(
            f"| {r.case_id} | {r.category} | {r.outcome} | {r.careless_outcome or '-'} | "
            f"{', '.join(r.controls) or '-'} | {', '.join(r.tool_calls) or '-'} |"
        )
    for r in results:
        if r.leaked or r.errors:
            print(f"- {r.case_id}: leaked={r.leaked} errors={r.errors}")


async def main(only: set[str] | None) -> None:
    settings = get_settings()
    configure_logging("WARNING", "console")
    cases = [c for c in load_cases() if only is None or c.id in only]
    model = build_agent_model(settings)
    name = getattr(model, "model", None) or getattr(model, "model_id", "fake")
    print(f"Agent model: {settings.llm_provider}/{name} | reasoning: {settings.agent_reasoning}")
    print(f"Cases: {len(cases)} | tool transport: {settings.agent_tool_transport}")
    engine = build_engine(settings.database_url)
    sessions = build_session_factory(engine)
    try:
        async with open_checkpointer(settings.database_url) as checkpointer:
            agent = AgentService(
                sessions=sessions,
                embeddings=build_embedding_provider(settings),
                model=model,
                checkpointer=checkpointer,
                settings=settings,
            )
            results = await run_red_team(agent, checkpointer, sessions, cases)
    finally:
        await engine.dispose()

    summary(results)
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"red-team-{stamp}.json"
    path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    print(f"\nDetails: {path.relative_to(REPORTS_DIR.parent)}")


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AI red-team evaluation of the agent")
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    args = parser.parse_args()
    selected = set(args.cases.split(",")) if args.cases else None
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(selected), loop_factory=loop_factory)
