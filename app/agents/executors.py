"""Where tool calls go: in-process, or through the MCP server (``mcp_client.py``).

The graph only knows this protocol, so the transport is a setting
(``AGENT_TOOL_TRANSPORT``), not a code change.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from app.tools.base import ToolContext, ToolResult, ToolSpec
from app.tools.catalog import read_tools_for
from app.tools.runtime import execute_tool


class ToolExecutor(Protocol):
    def specs(self) -> list[ToolSpec]:
        """The read-only tools the model may be offered (already filtered by role)."""
        ...

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class LocalToolExecutor:
    def __init__(self, ctx: ToolContext, thread_id: uuid.UUID | None = None) -> None:
        self._ctx = ctx
        self._thread_id = thread_id

    def specs(self) -> list[ToolSpec]:
        return read_tools_for(self._ctx.principal.role)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        return await execute_tool(self._ctx, name, arguments, thread_id=self._thread_id)
