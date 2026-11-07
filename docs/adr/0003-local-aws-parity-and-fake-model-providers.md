# ADR-0003: Local/AWS parity through adapters, and fake model providers

- Status: Accepted
- Date: 2026-11-07

## Context

Development must work from a clean checkout without deploying AWS infrastructure, CI must
not need AWS credentials or paid inference, and the total budget is ~USD 10. Yet the
staging environment uses S3, SQS and Bedrock.

## Decision

1. **Queue**: use the SQS API everywhere. Locally it is served by ElasticMQ; only
   `endpoint_url` changes. One implementation, real SQS semantics (visibility timeout,
   at-least-once delivery, dead-letter queue) during development.
2. **Object storage**: a small `Storage` interface with a local-filesystem adapter (dev,
   tests) and an S3 adapter (staging).
3. **Models**: `LLM_PROVIDER` / `EMBEDDING_PROVIDER` = `fake | bedrock`. Fake providers are
   deterministic (hash-based embeddings, scripted chat responses) so unit and integration
   tests are reproducible and free. Real Bedrock calls are opt-in and live in tests marked
   `@pytest.mark.aws`.
4. **Credentials**: never in `.env`. Locally the AWS SDK uses an SSO profile; in ECS it
   uses the task IAM role.

## Alternatives considered

- **LocalStack**: emulates S3 + SQS in one container, but adds weight and licensing
  uncertainty; ElasticMQ is tiny and SQS-faithful.
- **MinIO for S3**: community distribution changes make it a poor long-term default; a
  filesystem adapter is enough for local development.
- **PostgreSQL-backed job queue (`SKIP LOCKED`)**: fewer moving parts, but diverges from
  the SQS behaviour we deploy.

## Consequences

- Retrieval quality measured with fake embeddings is meaningless; quality benchmarks must
  always state the embedding/chat model used.
- Two storage adapters must be kept behaviourally equivalent (covered by shared tests).
