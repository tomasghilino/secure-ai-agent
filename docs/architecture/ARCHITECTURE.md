# Architecture — Northstar AI Operations Copilot

> Status: **proposed** (Day 0). Sections are refined as each milestone lands.
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

## 3. Ingestion pipeline (Day 1)

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

## 4. Query path — RAG (Day 2)

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

## 5. Agent orchestration (Day 3)

```mermaid
stateDiagram-v2
    [*] --> guard_input
    guard_input --> agent
    agent --> tools: tool_calls and steps < max_steps
    agent --> finalize: final answer / clarification / budget exhausted
    tools --> approval: sensitive tool (e.g. draft_customer_response)
    tools --> agent: read-only tool results
    approval --> agent: approved / rejected (resume after interrupt)
    finalize --> [*]
```

- **Explicit state** (`messages`, `tenant`, `roles`, `step_count`, `tool_calls_made`,
  `citations`, `pending_action`, `usage`).
- **Bounded loop**: `max_steps`, `max_tool_calls`, LangGraph `recursion_limit`,
  per-request token budget.
- **Tool authorization** happens in code before execution (role × tool matrix), not in
  the prompt.
- **Human-in-the-loop**: LangGraph `interrupt()` + PostgreSQL checkpointer; the API
  exposes approve/reject endpoints that resume the thread.
- **MCP**: read-only business tools live in a separate MCP server process and are
  discovered at runtime through `langchain-mcp-adapters`.

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
| Cross-tenant data leak | Tenant from verified JWT only; tenant filter in every query; PostgreSQL RLS |
| Unauthorized document access | Classification × role check applied inside the retrieval SQL |
| Prompt injection via documents | Retrieved text wrapped as delimited *untrusted data*; system prompt states it is never instructions; tools authorized in code |
| LLM-driven SQL abuse | No text-to-SQL; only parameterized, predefined, read-only queries; read-only DB role for tools |
| Sensitive actions | Approval interrupt; actions only *drafted*, never sent externally |
| Malicious uploads | Size limit, MIME + `%PDF-` magic bytes, page limit, PyMuPDF parse in worker |
| Secret leakage | No secrets in code/env files; AWS via SSO profile locally, IAM task role in AWS; log redaction |
| Cost abuse | Max output tokens, max agent steps, per-tenant daily token budget, rate limits |
| Lack of traceability | Audit log for tool calls and approvals; Langfuse traces |

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
