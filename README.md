# secure-ai-agent

[![CI](https://github.com/tomasghilino/secure-ai-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/tomasghilino/secure-ai-agent/actions/workflows/ci.yml)

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
  supervisor approves it; eligibility rules are enforced in code before and after approval.
- **Identity and limits**: JWT authentication with roles from claims, per-user rate limits
  and per-tenant daily token budgets.
- **Security controls**: tenant isolation with Row-Level Security, role-based tool
  authorization (deny by default), separation of duties, an append-only audit log and a
  kill switch. See the [threat model](docs/security/THREAT_MODEL.md), mapped to the OWASP
  Top 10 for LLM, agentic and MCP applications.
- **AI red team**: 17 attacks (prompt injection, cross-tenant probing, tool abuse...) run
  in CI; verdicts come from the database and tool trace ([results](docs/benchmarks/2026-11-22-red-team.md)).
- **Supply chain**: CI runs gitleaks, pip-audit, Trivy and produces an SBOM; actions are
  pinned by SHA and images by digest; the production image runs non-root and read-only.
- **AWS staging in Terraform**: API Gateway with a Cognito JWT authorizer in front of ECS
  Fargate tasks that have no route to the internet; VPC endpoints with data-perimeter
  policies, one customer-managed KMS key, least-privilege IAM and image pushes through OIDC
  ([decision](docs/adr/0011-private-staging-on-aws.md), [runbook and cost](docs/operations/staging.md)).
- **MCP server** (read-only, stdio) usable from any MCP client; the agent's MCP client
  pins tool definitions by hash.
- **Async ingestion**: PDF upload → queue → idempotent worker (extraction, chunking,
  embeddings with a cache).

## Tech stack

| Area | Tools |
| --- | --- |
| API | Python 3.12, FastAPI, Pydantic, PyJWT |
| Data | PostgreSQL 17, pgvector (HNSW), full-text search, SQLAlchemy 2 (async), Alembic |
| AI | LangChain, LangGraph, MCP Python SDK |
| Models | Ollama locally (Qwen3 8B, Qwen3-Embedding); Amazon Bedrock for staging (Claude Haiku, Titan Embeddings V2) |
| Infrastructure | Docker Compose, Terraform, ECS Fargate, API Gateway, Cognito, RDS, S3, SQS, KMS |
| Quality and security | pytest (unit, integration, real-model), ruff, mypy (strict), uv, GitHub Actions, gitleaks, pip-audit, Trivy |

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
make eval-security          # AI red-team suite against the agent
make scan                   # gitleaks, pip-audit and Trivy, as in CI
```

The API identifies callers with development headers by default. To use JWT
authentication, set the `AUTH_*` values shown in `.env.example` and get a token with
`make token role=supervisor user=sup-1`.

Real local models (NVIDIA GPU, ~8 GB VRAM): run `make llm-pull`, then set
`LLM_PROVIDER=ollama` and `EMBEDDING_PROVIDER=ollama` in `.env`. `make help` lists every
command.

## Documentation

- [Architecture](docs/architecture/ARCHITECTURE.md)
- [Threat model and security roadmap](docs/security/THREAT_MODEL.md)
- [Architecture decision records](docs/adr)
- [RAG evaluation results](docs/benchmarks/2026-11-19-rag-local.md)
- [Red-team and supply-chain results](docs/benchmarks/2026-11-22-red-team.md)
- [AWS staging runbook](docs/operations/staging.md)
- [Roadmap](docs/ROADMAP.md)
