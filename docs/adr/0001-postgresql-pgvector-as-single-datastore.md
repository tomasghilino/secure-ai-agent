# ADR-0001: PostgreSQL + pgvector as the single datastore

- Status: Accepted
- Date: 2026-11-07

## Context

The copilot needs (1) vector similarity search over document chunks, (2) keyword search,
(3) relational business data (orders, invoices, tickets), (4) agent checkpoints and audit
logs, and (5) strict tenant isolation. The project budget is ~USD 10 in total and the
AWS environment must be ephemeral.

## Decision

Use one PostgreSQL 17 database with the `pgvector` extension for everything:

- `vector(1024)` columns with an HNSW index (`vector_cosine_ops`) for semantic search.
- A generated `tsvector` column with a GIN index for full-text search.
- Ordinary tables for business data, ingestion jobs, LangGraph checkpoints and audit logs.
- Row-Level Security policies keyed on `tenant_id`.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Amazon OpenSearch Serverless | Minimum OCU capacity costs far more than the whole budget; excluded by constraint |
| Bedrock Knowledge Bases | Managed black box: hides chunking/retrieval we need to control and evaluate; excluded by constraint |
| Pinecone / Weaviate / Qdrant | Extra service, extra network hop, and a second place to enforce tenancy and RBAC |
| Chroma / FAISS in-process | Not a system of record; no transactions with business data, no RLS |

## Consequences

- Positive: hybrid search, metadata filters and tenancy are expressed in one SQL query and
  one transaction; one backup/restore story; cheap (RDS `db.t4g.micro` in staging).
- Positive: explicit SQL makes retrieval transparent and easy to explain.
- Negative: at tens of millions of vectors, a dedicated vector engine may scale better;
  HNSW indexes consume memory and slow down bulk inserts.
- Negative: approximate (HNSW) search combined with selective `WHERE` filters can return
  fewer than K rows; mitigated with pgvector iterative index scans and evaluation.
