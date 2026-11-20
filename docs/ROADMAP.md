# Roadmap

## Done

- Async, idempotent PDF ingestion (upload → queue → worker) with an embedding cache and
  one active version per document
- Hybrid retrieval (dense + full-text, weighted RRF) with tenant, status, point-in-time and
  classification filters in SQL
- Grounded answers with validated citations and an "insufficient evidence" path
- Evaluation set (47 questions, 7 categories) with deterministic retrieval and answer metrics
- Multi-tenancy enforced by query filters and PostgreSQL Row-Level Security
- LangGraph agent with read-only tools, role-based authorization, human approval
  (interrupt/resume with a PostgreSQL checkpointer) and an append-only audit log
- Read-only MCP server with client-side tool pinning
- Threat model mapped to the OWASP Top 10 for LLM, agentic and MCP applications

## Next

- JWT authentication with roles from claims; rate limits and per-tenant token budgets
- AI red-team evaluation (prompt injection, cross-tenant probing, tool abuse)
- CI: lint, types and tests, plus `pip-audit`, `gitleaks`, Trivy and an SBOM
- Hardened production container
- Terraform for an ephemeral AWS staging on Amazon Bedrock: least-privilege IAM, OIDC for
  CI, encryption at rest, VPC endpoints
- Faithfulness evaluation (Ragas or an LLM judge) and tracing
