# Architecture — Northstar AI Operations Copilot

> Sections are refined as each milestone lands.
> Significant decisions are recorded as ADRs in [`docs/adr`](../adr).

## 1. Problem and scope

Northstar Industrial (fictional) is an industrial equipment distributor with two legal
entities. Employees need one assistant that can:

1. Answer questions **grounded** in company documents (policies, contracts, catalogs),
   with verifiable citations (document, version, page).
2. Query **structured business data** (orders, invoices, tickets, suppliers) through
   safe, predefined tools — never free-form SQL.
3. Prepare **controlled workflows** (e.g. a customer response draft) that require a
   human approval before anything "sensitive" happens.

Non-goals: connecting to real external systems, building a UI beyond a minimal demo,
fine-tuning models.

## 2. System context

```mermaid
flowchart LR
    user([Employee]) -->|JWT: tenant + roles| api

    subgraph platform[Northstar AI Operations Copilot]
        api[FastAPI API]
        worker[Ingestion worker]
        agent[LangGraph agent]
        mcp[MCP server<br/>read-only business tools]
        db[(PostgreSQL 17<br/>pgvector + FTS + RLS)]
        store[(Object storage<br/>local FS / S3)]
        queue[[Queue<br/>ElasticMQ / SQS]]
    end

    api -->|upload PDF| store
    api -->|enqueue job| queue
    queue --> worker
    worker -->|read PDF| store
    worker -->|chunks + vectors| db
    api --> agent
    agent -->|hybrid retrieval| db
    agent -->|MCP tool calls| mcp
    mcp -->|parameterized SQL| db
    agent -->|Converse API| bedrock[[Amazon Bedrock<br/>chat model + Titan V2 embeddings]]
    worker -->|embeddings| bedrock
    agent -. traces .-> langfuse[[Langfuse]]
```

**Tenancy model.** Two tenants: `nsi-amer` (Northstar Industrial Americas) and
`nsi-emea` (Northstar Industrial EMEA). Every row of tenant data carries a `tenant_id`.
Isolation is enforced twice: in application queries **and** with PostgreSQL
Row-Level Security (defense in depth). The tenant always comes from the verified token,
never from model output or request bodies.

## 3. Ingestion pipeline

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as API
    participant S as Storage
    participant Q as Queue
    participant W as Worker
    participant B as Bedrock (Titan V2)
    participant P as PostgreSQL

    C->>A: POST /documents (PDF + metadata)
    A->>A: validate size, MIME, PDF magic bytes, sha256
    A->>P: look up (tenant, sha256) and (tenant, doc_key, version)
    alt same sha256 already indexed
        A-->>C: 200 already_indexed (idempotent no-op)
    else same doc_key + version with a different file
        A-->>C: 409 Conflict (nothing queued, nothing embedded)
    else new document or new version
        A->>S: put object (uploads/tenant/sha256.pdf)
        A->>P: insert ingestion_job (queued)
        A->>Q: send {job_id}
        A-->>C: 202 Accepted {job_id}
    end
    Q->>W: receive {job_id} (at-least-once)
    W->>P: claim job (queued → running), skip if already done
    W->>S: get PDF
    W->>W: extract text per page → normalize → chunk (page-aware)
    W->>P: look up embedding cache by content hash
    W->>B: embed only cache misses (batched)
    W->>P: insert chunks + vectors + tsvector (one transaction)
    W->>P: job → succeeded, previous version → superseded
    W->>Q: delete message
```

Key properties:

- **Asynchronous**: the API never parses or embeds inline; a slow or failing model call
  cannot block uploads.
- **Idempotent**: `(tenant_id, sha256)` deduplicates uploads; the worker re-checks job
  state because SQS delivers *at least once*; chunk writes are transactional.
- **Cost-aware**: embeddings are cached by `(content_hash, model_id, dimensions)`, so
  re-indexing unchanged text is free.
- **Failure handling**: visibility timeout + max receive count → dead-letter queue.

## 4. Query path — RAG

```mermaid
flowchart TD
    q[User question] --> g[Input guard<br/>size limits, injection heuristics]
    g --> e[Embed query]
    g --> k[Keyword query<br/>websearch_to_tsquery]
    e --> vs[pgvector top-K<br/>cosine distance, HNSW]
    k --> fts[Full-text top-K<br/>ts_rank_cd, GIN]
    vs --> f[Filters applied in SQL<br/>tenant · classification · status · effective date]
    fts --> f
    f --> rrf[Reciprocal Rank Fusion]
    rrf --> rr{Rerank?<br/>if justified by eval}
    rr --> ctx[Context builder<br/>token budget, delimited untrusted context, chunk IDs]
    ctx --> llm[Bedrock Converse<br/>grounded-answer prompt]
    llm --> cv[Citation validator<br/>only chunk IDs that were provided]
    cv --> ans[Answer + citations<br/>doc name, doc ID, version, page]
    cv -->|insufficient evidence| idk[Explicit 'not found in sources']
```

Every stage is a separate, testable component (`app/rag/*`). Retrieval is evaluated
**independently** of generation (recall@k / precision@k / MRR on a labelled dataset).

## 5. Agent orchestration

```mermaid
stateDiagram-v2
    [*] --> agent
    agent --> tools: tool calls and steps < max_steps
    agent --> step_limit: tool calls and steps = max_steps
    agent --> [*]: final answer
    tools --> agent: read-only results
    tools --> approval: request_return_authorization (pending action)
    approval --> agent: approved (RMA created) / rejected
    step_limit --> [*]
```

- **State**: `messages`, `steps`, `pending_action`, `stop_reason`, saved after every step
  by the PostgreSQL checkpointer (schema `agent_checkpoints`) under the thread id.
- **Identity**: the requester's tenant, user and role come from the request and live
  outside the state; the run resumes as the requester after approval.
- **Bounded loop**: `AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS_PER_STEP`,
  `AGENT_TOOL_RESULT_MAX_CHARS`, LangGraph `recursion_limit`.
- **Tool authorization** in code (role × tool, deny by default), re-checked on every call;
  RLS underneath. Every call and decision lands in the append-only `audit_events`.
- **Human-in-the-loop**: `interrupt()` in the approval node; `POST
  /v1/agent/threads/{id}/decision` (supervisor, not the requester) resumes it.
- **MCP**: the same read-only tools behind an MCP server over stdio (`python -m
  app.mcp_server`); with `AGENT_TOOL_TRANSPORT=mcp` the agent starts it as a subprocess
  and accepts only tools whose hash matches the reviewed catalog (ADR-0008).

## 6. Data model (preliminary)

```mermaid
erDiagram
    TENANT ||--o{ DOCUMENT : owns
    DOCUMENT ||--o{ CHUNK : "split into"
    DOCUMENT ||--o{ INGESTION_JOB : "processed by"
    TENANT ||--o{ CUSTOMER : has
    CUSTOMER ||--o{ ORDER : places
    ORDER ||--o{ INVOICE : billed_by
    CUSTOMER ||--o{ SUPPORT_TICKET : opens
    TENANT ||--o{ SUPPLIER : contracts
    TENANT ||--o{ AUDIT_LOG : records
    TENANT ||--o{ LLM_USAGE : meters

    DOCUMENT {
        uuid id
        text tenant_id
        text doc_key "e.g. NSI-POL-PRC-001"
        text version
        date effective_date
        text status "active | superseded | draft"
        text classification
        text sha256
    }
    CHUNK {
        uuid id
        int page_number
        text section
        text content
        vector embedding "vector(1024)"
        tsvector tsv
        jsonb metadata
    }
```

## 7. Security architecture (summary)

| Threat | Control |
| --- | --- |
| Forged identity | JWT verified against the provider's JWKS (asymmetric algorithms, issuer, audience, expiry); one role from claims |
| Cross-tenant data leak | Tenant from verified JWT only; tenant filter in every query; PostgreSQL RLS |
| Unauthorized document access | Classification × role check applied inside the retrieval SQL |
| Prompt injection via documents | Retrieved text wrapped as delimited *untrusted data*; system prompt states it is never instructions; tools authorized in code |
| LLM-driven SQL abuse | No text-to-SQL; only parameterized, predefined, read-only queries; read-only DB role for tools |
| Sensitive actions | Approval interrupt; actions only *drafted*, never sent externally |
| Malicious uploads | Size limit, MIME + `%PDF-` magic bytes, page limit, PyMuPDF parse in worker |
| Secret leakage | No secrets in code/env files; AWS via SSO profile locally, IAM task role in AWS; log redaction |
| Cost abuse | Max output tokens, max agent steps, per-tenant daily token budget, rate limits |
| Lack of traceability | Audit log for tool calls and approvals; Langfuse traces |
| Supply chain | CI: gitleaks, pip-audit, Trivy, SBOM; actions pinned by SHA, images by digest; hardened non-root runtime image |

Full threat model, test evidence and residual risks: [THREAT_MODEL.md](../security/THREAT_MODEL.md).

## 8. Local vs AWS staging

| Concern | Local (Docker Compose) | AWS staging (ephemeral) |
| --- | --- | --- |
| API / worker / MCP | Containers with hot reload | ECS Fargate tasks (images in ECR) |
| Database | `pgvector/pgvector:0.8.7-pg17` | RDS PostgreSQL 17 `db.t4g.micro`, single-AZ |
| Object storage | Local filesystem adapter | S3 (private, SSE-S3) |
| Queue | ElasticMQ (SQS-compatible API) | SQS + DLQ |
| LLM / embeddings | `fake` providers or Bedrock via SSO profile | Bedrock via IAM task role |
| Secrets | `.env` (no AWS keys) | Secrets Manager |
| Logs / traces | stdout (structlog) + optional Langfuse | CloudWatch Logs + optional Langfuse |

AWS constraints: no NAT Gateway (public subnets + security groups), no OpenSearch,
no Bedrock Knowledge Bases, no Provisioned Throughput, everything tagged and destroyed
after the demo. Infrastructure is described in Terraform with local state.
