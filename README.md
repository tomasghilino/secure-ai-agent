# Northstar AI Operations Copilot

An enterprise AI assistant for a (fictional) industrial equipment distributor: it answers
questions grounded in company documents with verifiable citations, queries business data
through safe, predefined tools, and prepares sensitive actions for human approval.

> **Status: work in progress (Days 1–3 of 4 done).** See the
> [delivery plan](docs/PROJECT_PLAN.md) and the [architecture](docs/architecture/ARCHITECTURE.md).

## Capabilities

Done:
- Asynchronous, idempotent PDF ingestion: upload → storage → queue (SQS API) → worker,
  with an embedding cache and one active version per document
- Hybrid retrieval (pgvector + PostgreSQL full-text + weighted Reciprocal Rank Fusion)
  with tenant, status, effective-date (point-in-time) and classification filters in SQL
- Grounded answers with validated citations (document, ID, version, page) and an explicit
  "insufficient evidence" path
- Multi-tenancy enforced twice: tenant filters and PostgreSQL Row-Level Security
- Evaluation on a labelled set of 47 questions: retrieval metrics and answer checks
  ([results](docs/benchmarks/2026-11-19-rag-local.md))
- LangGraph operations agent over 8 read-only tools, with a bounded loop and human approval
  (interrupt/resume, PostgreSQL checkpointer) for the one write it can lead to
- Role × tool authorization, separation of duties and an append-only audit log
- Read-only MCP server (stdio) usable from any MCP client; the agent's MCP client pins tools
- Security by design: [threat model](docs/security/THREAT_MODEL.md) mapped to the OWASP Top 10
  for LLM, agentic and MCP applications

Planned:
- Authentication (JWT) and roles from claims, rate limits, AI red-team evaluation
- CI with security scanning; Terraform for an ephemeral AWS staging on Amazon Bedrock

## Local quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), GNU Make. Optional: an NVIDIA
GPU (≈ 8 GB VRAM) to run the local models.

```bash
make setup     # Python 3.12 deps + .env (fake model providers: no AWS, no cost)
make dev       # PostgreSQL+pgvector, ElasticMQ and the API on http://localhost:8000
make check     # lint + type check + unit tests

# Optional: real local models on the GPU (free), then set *_PROVIDER=ollama in .env
make llm-pull  # starts Ollama and downloads qwen3:8b + qwen3-embedding:0.6b (~6 GB)
make test-llm  # real-model tests
```

Model providers are configuration: `fake` (tests/CI), `ollama` (local GPU) and `bedrock`
(AWS staging). See [ADR-0005](docs/adr/0005-local-first-model-providers-with-ollama.md).

Then, with the stack running:

```bash
make migrate   # create the tables
make seed      # business records (customers, orders, invoices, tickets, suppliers)
make data      # generate the 12 synthetic PDFs
make ingest    # extract, chunk, embed and store them (idempotent)
make search q="how many days do I have to return a product?"
make ask q="how many days do I have to return a product?"   # answer with citations
make eval-retrieval   # dense vs keyword vs hybrid on the evaluation set
make eval-answers     # answered / refused / cited the right section

# The operations agent (thinking on; AGENT_REFERENCE_DATE=2026-11-15 matches the data)
make agent q="Customer C-1006 asks to return SO-2026-0103. Is it allowed?"
make agent q="Please create a return authorization for SO-2026-0101, wrong size."
make approve t=<thread_id>          # as supervisor sup-1 (add reject=1 to reject)
make agent q="Which invoices are overdue?" role=finance_analyst user=fin-1
```

Use the read-only MCP server from Claude Code (or any MCP client):

```bash
claude mcp add northstar -- uv run --directory /path/to/repo python -m app.mcp_server --tenant nsi-amer --role support_agent
```

The API (Swagger at http://localhost:8000/docs) exposes `POST /v1/documents` (upload),
`GET /v1/jobs/{id}`, `POST /v1/search`, `POST /v1/ask` and the agent endpoints
(`/v1/agent/threads`, `/v1/agent/approvals`, `/v1/audit-events`). Until authentication
arrives, identity comes from dev-only headers (`X-Tenant-ID`, `X-User-ID`, `X-User-Role`),
rejected outside local and test.

`make help` lists every command.

## Documentation

- [Architecture](docs/architecture/ARCHITECTURE.md)
- [Architecture Decision Records](docs/adr)
- [Threat model and security roadmap](docs/security/THREAT_MODEL.md)
- [Delivery plan](docs/PROJECT_PLAN.md)
