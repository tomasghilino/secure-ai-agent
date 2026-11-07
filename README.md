# Northstar AI Operations Copilot

An enterprise AI assistant for a (fictional) industrial equipment distributor: it answers
questions grounded in company documents with verifiable citations, queries business data
through safe, predefined tools, and prepares sensitive actions for human approval.

> **Status: work in progress (Day 1 of 4).** See the [delivery plan](docs/PROJECT_PLAN.md)
> and the [architecture](docs/architecture/ARCHITECTURE.md).

## Planned capabilities

- Asynchronous, idempotent PDF ingestion (S3/SQS on AWS, local adapters in development)
- Hybrid retrieval (pgvector + PostgreSQL full-text + Reciprocal Rank Fusion) with
  tenant, role and effective-date filtering
- Grounded answers with citations (document, ID, version, page) on Amazon Bedrock
- LangGraph agent with bounded tool use, MCP-served business tools and
  human-in-the-loop approval
- Evaluation harness (retrieval metrics, groundedness), tracing and cost tracking
- Multi-tenancy with PostgreSQL Row-Level Security; Terraform for an ephemeral AWS staging

## Local quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), GNU Make.

```bash
make setup    # Python 3.12 deps + .env (fake model providers: no AWS, no cost)
make dev      # PostgreSQL+pgvector, ElasticMQ and the API on http://localhost:8000
make check    # lint + type check + unit tests
```

`make help` lists every command.

## Documentation

- [Architecture](docs/architecture/ARCHITECTURE.md)
- [Architecture Decision Records](docs/adr)
- [Delivery plan](docs/PROJECT_PLAN.md)
