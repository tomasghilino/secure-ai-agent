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
- JWT authentication with roles from claims; an editor role for the knowledge base
- Per-user rate limits and per-tenant daily token budgets (atomic counters in PostgreSQL)
- Request IDs, security headers and error responses that reveal nothing
- AI red-team suite (17 attacks) in CI; eligibility rules enforced in code at proposal and
  approval
- CI: lint, types, unit and integration tests, gitleaks, pip-audit, Trivy, SBOM,
  actionlint; actions pinned by SHA, images by digest; Dependabot
- Hardened production image (non-root, read-only root filesystem, no package manager)

## Next

- Red-team and faithfulness results with real models (Qwen3 locally, Claude on Bedrock)
- Terraform for an ephemeral AWS staging on Amazon Bedrock: Cognito as the JWT issuer,
  least-privilege IAM, OIDC for CI, encryption at rest, VPC endpoints, WAF rate limiting
- Tracing (OpenTelemetry or Langfuse) correlated with the request ID
