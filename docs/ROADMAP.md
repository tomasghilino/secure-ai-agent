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
- Terraform for a private, disposable AWS staging: API Gateway with a Cognito JWT
  authorizer, ECS Fargate without an internet route, VPC endpoints with data-perimeter
  policies, a customer-managed KMS key, least-privilege IAM, image pushes through OIDC

- Staging applied, verified (16 checks, perimeter probes, IAM Access Analyzer) and
  destroyed

## Next

- Bedrock models on staging, once the account's quotas are restored
- Red-team and faithfulness results with real models (Qwen3 locally, Claude on Bedrock)
- A database login role with IAM authentication instead of the RDS master user
- Tracing (OpenTelemetry or Langfuse) correlated with the request ID
