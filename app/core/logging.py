"""Structured logging.

Logs are key/value events (`log.info("ingestion.chunked", document_id=..., chunks=12)`)
rendered as JSON in deployed environments so CloudWatch Logs Insights can query them,
and as readable console output locally. Context such as `request_id` or `tenant_id` is
bound once per request via contextvars and attached to every event automatically.

Rule: never log full document contents, prompts with customer data, or secrets.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog


def configure_logging(
    level: str = "INFO", fmt: str = "console", stream: TextIO | None = None
) -> None:
    """``stream`` defaults to stdout; the MCP server passes stderr (stdout carries the protocol)."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: list[Any]
    if fmt == "json":
        renderer = [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
    else:
        renderer = [structlog.dev.ConsoleRenderer()]

    structlog.configure(
        processors=[*shared, *renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(stream or sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
