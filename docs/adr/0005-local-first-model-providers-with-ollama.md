# ADR-0005: Local-first model providers with Ollama

- Status: Accepted
- Date: 2026-11-08
- Related: [ADR-0003](0003-local-aws-parity-and-fake-model-providers.md),
  [ADR-0004](0004-bedrock-model-selection-and-aws-account.md)

## Context

Bedrock quotas are 0 in every available AWS account until AWS Support restores them
(ADR-0004), and the project has four days. The developer machine has an NVIDIA RTX 3080 Ti
(12 GB VRAM) and Docker Desktop with working GPU passthrough. Fake providers keep tests
free, but they cannot show real retrieval quality or real tool calling.

## Decision

Add a third provider, `ollama`, and make it the default for local development:

| Role | Local (Ollama, GPU) | Staging (Bedrock) |
| --- | --- | --- |
| Chat / agent | `qwen3:8b` (Apache-2.0, native tool calling, 5.2 GB) | `us.anthropic.claude-haiku-5-5` |
| Embeddings | `qwen3-embedding:0.6b` (Apache-2.0, 1024 dims, 639 MB) | `amazon.titan-embed-text-v2:0` (1024 dims) |

- Ollama runs as a Compose service behind the opt-in `llm` profile (GPU reservation), so
  machines without a GPU still run everything with fake providers.
- Model choice favours well-known vendors with a clear licence over newer models with
  unclear provenance: licence and supply chain matter in an enterprise setting.
- Both embedding models produce 1024-d vectors, so the database schema does not change
  between environments. Vectors are **not** interchangeable across models: switching the
  embedding model requires re-embedding (the model ID is stored with each chunk).
- Qwen3-Embedding is asymmetric: queries get a task instruction, documents do not.

## Measured on this machine (2026-11-08)

- Both models fit fully in VRAM (`ollama ps`: 100% GPU, ~8.5 GB used with the desktop).
- `qwen3:8b`, thinking disabled: ~53 output tokens/s warm; first request ~40 s (cold load).
- Tool-calling smoke test: "What is the status of order ORD-10023?" →
  `get_order_status({"order_id": "ORD-10023"})`.
- Paraphrase test: "How many days does a client have to send back a product?" ranks the
  30-day returns clause first (cosine 0.59) with `qwen3-embedding:0.6b`; the fake provider
  scores it 0.19 and ranks an unrelated warranty sentence second.

## Alternatives considered

- **Wait for Bedrock**: blocks Days 1–3 on an external ticket.
- **Another hosted API (OpenAI, Anthropic direct)**: new account, key and billing; does not
  remove the Bedrock work for staging.
- **In-process embeddings (sentence-transformers / fastembed)**: no extra container, but
  pulls heavy ML dependencies into the API image and does not cover the chat model.
- **Larger local models (14B+)**: better quality but would not leave VRAM for embeddings
  and a usable context window.

## Consequences

- Development is free, private and not blocked by AWS.
- An 8B model is weaker than Claude Haiku 5.5 at tool selection and grounding; prompts and
  guards must be robust, and quality benchmarks must name the model that produced them.
- Ollama's VRAM-based default context is 4096 tokens; RAG requests set `num_ctx`
  explicitly.
- AWS staging still requires Bedrock (Fargate has no GPU), so the quota ticket remains open.
