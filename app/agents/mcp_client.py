"""The agent's tools through MCP, with tool pinning (OWASP MCP03, ASI04).

An MCP server declares its tools (name, description, input schema) and the model reads
those descriptions as instructions. A malicious or compromised server can hide
instructions in a description ("tool poisoning") or change it after it was reviewed
("rug pull"). So the client does not trust the listing: every tool must match the
reviewed definition in our catalog, by hash, or the connection is refused (the same
idea as the integrity hashes in a lockfile).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import mcp_types as types
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.lowlevel import Server

from app.core.config import Settings
from app.tools.authorization import Principal, is_allowed
from app.tools.base import ToolResult, ToolSpec, tool_fingerprint
from app.tools.catalog import CATALOG

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# Settings the server process needs. Passed explicitly: the MCP SDK starts the child
# with a minimal environment, and secrets travel in the environment, never in the
# command line (visible to every user in the process list).
_FORWARDED_SETTINGS = (
    "app_env",
    "database_url",
    "embedding_provider",
    "embedding_dimensions",
    "ollama_base_url",
    "ollama_embedding_model",
    "aws_region",
    "aws_profile",
    "bedrock_embedding_model_id",
    "rag_retrieval_mode",
    "agent_reference_date",
    "log_level",
)


class ToolPinningError(RuntimeError):
    """The server declared a tool that does not match the reviewed catalog."""


def verify_pins(listed: Sequence[types.Tool]) -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    for tool in listed:
        spec = CATALOG.get(tool.name)
        if spec is None:
            raise ToolPinningError(f"unexpected tool {tool.name!r}")
        declared = tool_fingerprint(tool.name, tool.description, dict(tool.input_schema))
        if declared != spec.fingerprint:
            raise ToolPinningError(f"tool {tool.name!r} differs from its reviewed definition")
        specs.append(spec)
    return specs


def stdio_server_parameters(principal: Principal, settings: Settings) -> StdioServerParameters:
    env = {
        name.upper(): str(value)
        for name in _FORWARDED_SETTINGS
        if (value := getattr(settings, name)) is not None
    }
    env |= {
        "MCP_TENANT_ID": principal.tenant_id,
        "MCP_USER_ID": principal.user_id,
        "MCP_ROLE": principal.role,
    }
    return StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], env=env, cwd=REPOSITORY_ROOT
    )


class McpToolExecutor:
    """A ``ToolExecutor`` that calls the read-only MCP server.

    ``server`` is the launch command (production path: a stdio subprocess) or, in tests,
    a ``Server`` instance served in-process.
    """

    def __init__(self, server: StdioServerParameters | Server[Any], principal: Principal) -> None:
        self._client = Client(server)
        self._principal = principal
        self._specs: list[ToolSpec] = []

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        try:
            listed = await self._client.list_tools()
            self._specs = verify_pins(listed.tools)
        except BaseException:
            await self._client.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    def specs(self) -> list[ToolSpec]:
        return [spec for spec in self._specs if is_allowed(self._principal.role, spec.name)]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in {spec.name for spec in self.specs()}:
            return ToolResult(name, "denied", error=f"tool {name} is not available")
        result = await self._client.call_tool(name, arguments)
        if isinstance(result.structured_content, dict):
            return ToolResult.from_dict(result.structured_content)
        text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
        try:
            return ToolResult.from_dict(json.loads(text))
        except (ValueError, AttributeError):
            return ToolResult(name, "error", error="malformed tool result")
