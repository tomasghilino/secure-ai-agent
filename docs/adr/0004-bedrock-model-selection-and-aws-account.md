# ADR-0004: Bedrock model selection and AWS account

- Status: Accepted (Bedrock access pending quota restoration)
- Date: 2026-11-08

## Context

The project needs a low-cost chat model with reliable tool calling, an embedding model, and
optionally a reranker, all on Amazon Bedrock in `us-east-1`, within a ~USD 10 total budget.
Prices below come from the **AWS Price List API** (`AmazonBedrock` and
`AmazonBedrockFoundationModels` service codes, region `us-east-1`) on 2026-11-08.

| Model | Access in us-east-1 | Input / output (USD per 1M tokens) | Notes |
| --- | --- | --- | --- |
| Claude Haiku 5.5 | Inference profile (`us.` / `global.`) | 0.11 / 0.55 (US) · 0.10 / 0.50 (global) | Strong tool calling; needs Anthropic first-time-use form |
| Claude Haiku 4.5 | Inference profile | 1.10 / 5.50 (US) · 1.00 / 5.00 (global) | ~10x Haiku 5.5 for this use case |
| Amazon Nova Micro | On-demand + profile | 0.035 / 0.14 | Text only; cheapest baseline |
| Amazon Nova Lite | On-demand + profile | 0.06 / 0.24 | Cheap multimodal baseline |
| Amazon Nova 2 Lite | Inference profile only | 0.33 / 2.75 | More expensive; lifecycle date close (EOL no sooner than 2026-12-02) |
| Titan Text Embeddings V2 | On-demand | 0.02 (input only) | 256/512/1024 dims, normalized output |
| Cohere Rerank 3.5 | On-demand | priced per query | Candidate reranker |

## Decision

- **AWS account**: an existing workload account (Workloads OU of a personal AWS
  Organization), region `us-east-1`. The region holds almost no pre-existing resources;
  whatever exists is never touched by the project. All project resources carry
  `Project=northstar-ai-operations-copilot` tags.
- **Chat / agent model**: Claude Haiku 5.5 through the **US geo inference profile**
  `us.anthropic.claude-haiku-5-5` (requests stay within US Regions — a data-residency
  choice worth ~10% over the global profile).
- **Baseline for model comparison** (optional): Amazon Nova Micro.
- **Embeddings**: `amazon.titan-embed-text-v2:0`, 1024 dimensions, normalized.
- **Reranker**: decided from measured retrieval gains.
- Model IDs are configuration (`BEDROCK_CHAT_MODEL_ID`, `BEDROCK_EMBEDDING_MODEL_ID`).

## Why not a new dedicated member account

A dedicated account gives the cleanest isolation and can be closed after the demo, but every
account in this organization — including the oldest — has **applied Bedrock quotas of 0**
(AWS defaults are 300k TPM for Titan V2 and 10M TPM for Haiku 5.5). A brand-new account
would very likely start at 0 as well, adding a root email and SSO assignment without
removing the blocker. Tag-based attribution, local Terraform state and the cleanup audit
script provide the isolation guarantees the project needs.

## Findings that shaped the decision

- A 4-token Titan V2 request returned `ThrottlingException` in two different workload
  accounts: quotas of 0 are enforced, not just displayed.
- Service Quotas only accepts increase requests **above the AWS default**; restoring a
  zeroed quota to its default requires an AWS Support "service limit increase" case.
- The account uses the Basic support plan, so the case must be opened from the console
  (the Support API requires a premium plan).

## Cost expectation

A generous development + evaluation volume (≈ 1,500 chat calls × 3k input + 300 output
tokens) costs ≈ 4.5M × 0.11 + 0.45M × 0.55 ≈ **USD 0.75** with Haiku 5.5 (US profile).
Embedding the whole corpus (≈ 30k tokens) costs ≈ USD 0.0006. Most of the budget is left for
the ephemeral staging infrastructure.

## Consequences

- Until the quotas are restored, development uses the deterministic fake providers
  (ADR-0003); local development is not blocked.
- If Titan V2 is not restored in time, a local embedding model is the documented fallback,
  and every benchmark must state which embedding model produced it.
