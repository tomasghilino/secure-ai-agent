# ADR-0002: Async Python stack (FastAPI, SQLAlchemy 2, psycopg 3) managed with uv

- Status: Accepted
- Date: 2026-11-07

## Context

The service is I/O bound: database queries, model calls, MCP calls and queue polling.
LangGraph's PostgreSQL checkpointer and the MCP client libraries are async-first. Several
current libraries (SQLAlchemy 2.1, PyMuPDF 2.x, pydantic-settings 2.16) require
Python >= 3.11.

## Decision

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async) with the psycopg 3 driver.
- boto3 (Bedrock, S3, SQS) is synchronous: calls run in worker threads
  (`asyncio.to_thread` / LangChain's async wrappers) so they never block the event loop.
- `uv` for dependency management with a committed `uv.lock` (reproducible installs,
  fast Docker builds with a cached dependency layer).
- Dependencies are added incrementally, each in the change that first needs it.

## Alternatives considered

- **Sync FastAPI + threadpool**: simpler, but would fight the async-only MCP adapters and
  the async LangGraph checkpointer.
- **asyncpg driver**: fast, but LangGraph's checkpointer requires psycopg 3; two drivers
  would mean two connection-pool configurations.
- **pip + requirements.txt / Poetry**: workable; uv gives lockfile + Python version
  management + speed with less tooling.

## Consequences

- psycopg's async mode cannot use Windows' default `ProactorEventLoop`; host-side test
  runs on Windows must use the selector event loop (handled in test configuration).
  Containers and CI run Linux, where this does not apply.
- Async code requires discipline: no blocking calls in request handlers (enforced in part
  by Ruff's `ASYNC` rules).
