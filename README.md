# secure-ai-agent

An AI operations agent for a fictional industrial distributor, built security-first. It
answers questions from company documents with verified citations, looks up business data
through read-only tools, and never changes anything without a human approval.

## Highlights

- **Hybrid RAG**: pgvector + PostgreSQL full-text search fused with Reciprocal Rank Fusion;
  answers cite document, version and page, and refuse when the evidence is missing.
  Measured on a labelled set of 47 questions ([results](docs/benchmarks/2026-11-19-rag-local.md)).
- **LangGraph agent** with a bounded loop over 8 read-only tools (orders, tickets,
  invoices, suppliers, policies).
- **Human in the loop**: the only write (a return authorization) pauses the run until a
  supervisor approves it; state is persisted in PostgreSQL.
- **Security controls**: tenant isolation with Row-Level Security, role-based tool
  authorization (deny by default), separation of duties, an append-only audit log and a
  kill switch. See the [threat model](docs/security/THREAT_MODEL.md), mapped to the OWASP
  Top 10 for LLM, agentic and MCP applications.
- **MCP server** (read-only, stdio) usable from any MCP client; the agent's MCP client
  pins tool definitions by hash.
- **Async ingestion**: PDF upload → queue → idempotent worker (extraction, chunking,
  embeddings with a cache).

## Tech stack

| Area | Tools |
| --- | --- |
| API | Python 3.12, FastAPI, Pydantic |
| Data | PostgreSQL 17, pgvector (HNSW), full-text search, SQLAlchemy 2 (async), Alembic |
| AI | LangChain, LangGraph, MCP Python SDK |
| Models | Ollama locally (Qwen3 8B, Qwen3-Embedding); Amazon Bedrock for staging (Claude Haiku, Titan Embeddings V2) |
| Infrastructure | Docker Compose, SQS API (ElasticMQ locally) |
| Quality | pytest (unit, integration, real-model), ruff, mypy (strict), uv |

## Run it locally

Requirements: Docker, [uv](https://docs.astral.sh/uv/) and GNU Make. Models default to
offline fakes, so no GPU or cloud account is needed.

```bash
make setup                  # install dependencies and create .env
make up migrate seed        # PostgreSQL + queue, schema, business data
make data ingest            # generate the sample PDFs and index them
make ask q="How many days do I have to return a product?"
make agent q="Can customer C-1006 return order SO-2026-0103?"
make dev                    # full stack; API docs at http://localhost:8000/docs
make check                  # lint, type check, unit tests
```

Real local models (NVIDIA GPU, ~8 GB VRAM): run `make llm-pull`, then set
`LLM_PROVIDER=ollama` and `EMBEDDING_PROVIDER=ollama` in `.env`. `make help` lists every
command.

## Documentation

- [Architecture](docs/architecture/ARCHITECTURE.md)
- [Threat model and security roadmap](docs/security/THREAT_MODEL.md)
- [Architecture decision records](docs/adr)
- [RAG evaluation results](docs/benchmarks/2026-11-19-rag-local.md)
- [Roadmap](docs/ROADMAP.md)
