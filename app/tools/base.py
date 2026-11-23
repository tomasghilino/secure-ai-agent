"""Building blocks shared by every tool: inputs, results, context and the tool spec."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.rag.embeddings import EmbeddingProvider
from app.rag.retrieval import Mode
from app.tools.authorization import Principal

ToolStatus = Literal["ok", "denied", "error"]


class StrictInput(BaseModel):
    """Tool arguments come from the model: untrusted input, validated like a request body.

    ``extra="forbid"`` rejects arguments the schema does not declare (a model cannot slip
    in ``tenant_id``), and every field carries a pattern or a length limit (OWASP LLM05).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolError(Exception):
    """An expected failure (not found, not eligible) reported to the model as text."""


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Trusted inputs of a tool call: everything that does NOT come from the model."""

    sessions: async_sessionmaker[AsyncSession]
    embeddings: EmbeddingProvider
    principal: Principal
    today: date
    retrieval_mode: Mode = "hybrid"
    # The tenant's return window in days (enforced copy of its returns policy).
    # None: returns cannot be executed for this tenant (deny by default).
    return_window_days: int | None = None


def _json_default(value: object) -> str:
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def to_json(value: object) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    status: ToolStatus
    data: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe: what the model, the MCP client and the tests receive."""
        body: dict[str, Any] = {"tool": self.tool, "status": self.status}
        if self.status == "ok":
            body["data"] = self.data
        else:
            body["error"] = self.error
        result: dict[str, Any] = json.loads(to_json(body))
        return result

    def to_json(self) -> str:
        return to_json(self.as_dict())

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> ToolResult:
        status = body.get("status")
        if status not in ("ok", "denied", "error"):
            return cls(str(body.get("tool")), "error", error="malformed tool result")
        return cls(str(body.get("tool")), status, body.get("data"), body.get("error"))


Handler = Callable[[AsyncSession, Any, ToolContext], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool: its contract (name, description, input schema) and its implementation."""

    name: str
    description: str
    input_model: type[StrictInput]
    handler: Handler

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def as_model_tool(self) -> dict[str, Any]:
        """OpenAI-style function definition: what ``bind_tools`` sends to the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    @property
    def fingerprint(self) -> str:
        return tool_fingerprint(self.name, self.description, self.input_schema)


def tool_fingerprint(name: str, description: str | None, input_schema: dict[str, Any]) -> str:
    """Hash of everything a model reads about a tool: a changed description is a new tool.

    The MCP client compares it with the catalog to detect tool poisoning or a "rug pull"
    (a server that changes a tool's description after it was reviewed).
    """
    canonical = json.dumps(
        {"name": name, "description": description or "", "input_schema": input_schema},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
