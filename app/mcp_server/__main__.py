"""Entry point: ``python -m app.mcp_server`` (``make mcp``), served over stdio.

The identity comes from ``MCP_TENANT_ID`` / ``MCP_ROLE`` / ``MCP_USER_ID`` or the same
values as flags (``--tenant nsi-amer --role support_agent``). Whoever launches the process
chooses it: fine for a local stdio server (same trust as running the CLI), not for a
remote one, which would need OAuth and per-user tokens instead.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from typing import Any

from mcp.server.stdio import stdio_server

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import build_engine, build_session_factory
from app.mcp_server.server import build_server, principal_from_settings
from app.rag.embeddings import build_embedding_provider
from app.tools.authorization import ROLES
from app.tools.base import ToolContext


async def serve(overrides: dict[str, Any]) -> None:
    # Validated like any setting; flags win over the environment.
    settings = Settings.model_validate({**get_settings().model_dump(), **overrides})
    # stdout carries the MCP protocol: logs must go to stderr.
    configure_logging(settings.log_level, "json", stream=sys.stderr)
    principal = principal_from_settings(settings)
    engine = build_engine(settings.database_url)
    ctx = ToolContext(
        sessions=build_session_factory(engine),
        embeddings=build_embedding_provider(settings),
        principal=principal,
        today=settings.agent_reference_date or date.today(),
        retrieval_mode=settings.rag_retrieval_mode,
    )
    server = build_server(ctx)
    get_logger(__name__).info("mcp.start", tenant=principal.tenant_id, role=principal.role)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Northstar read-only MCP server (stdio)")
    parser.add_argument("--tenant", dest="mcp_tenant_id")
    parser.add_argument("--role", dest="mcp_role", choices=ROLES)
    parser.add_argument("--user", dest="mcp_user_id")
    flags = {k: v for k, v in vars(parser.parse_args()).items() if v is not None}
    # psycopg's async mode needs the selector event loop on Windows (ADR-0002).
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(serve(flags), loop_factory=loop_factory)
