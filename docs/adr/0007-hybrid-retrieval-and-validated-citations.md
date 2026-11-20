# ADR-0007: Hybrid retrieval with weighted RRF, and answers with validated citations

- Status: Accepted
- Date: 2026-11-19

## Context

This step turns search into question answering. Two decisions shape it:

1. **How to retrieve.** Embeddings understand paraphrases and other languages but blur
   exact identifiers (NX-4400 vs NX-4400B, form numbers). PostgreSQL full-text search is
   exact but blind to synonyms. The usual answer is hybrid search with Reciprocal Rank
   Fusion (RRF), but whether it helps must be measured, not assumed.
2. **How much to trust the model.** A chat model can answer from memory, invent sources or
   follow instructions planted in a document. Answers must be checkable by a person.

## Decision

**Retrieval** (`app/rag/retrieval.py`):
- Dense (pgvector, HNSW) and keyword (`tsvector` generated column + GIN, `ts_rank_cd`) run
  with the same eligibility filter in SQL: tenant, active version or version in force on
  a date (`as_of`), classification, document keys.
- Hybrid fuses both top-20 lists with RRF (k = 60). The keyword ranking weighs **0.5**:
  with equal weights the evaluation dropped from 0.98 (dense) to 0.90 hit@5, because
  chunks ranked mediocrely by both lists outscored a chunk ranked first by one of them
  (`ts_rank_cd` has no IDF, so common words make the keyword list noisy).
- The mode is a setting (`RAG_RETRIEVAL_MODE`); hybrid stays the default as insurance for
  identifiers and for other embedding models (Titan V2 in staging), although dense alone
  scored slightly higher on this set (see docs/benchmarks).
- No reranker: no local or regional model to justify yet; retrieval already reaches
  0.95–0.98 hit@5. Revisit if the evaluation set grows and precision drops.

**Generation** (`app/rag/answer.py`, `app/rag/context.py`):
- Sources are selected in rank order within a token budget, numbered and wrapped in
  `<source>` tags; tag-like text inside documents is neutralised.
- The prompt demands a citation `[n]` per fact, only the given numbers, and the exact word
  `INSUFFICIENT_EVIDENCE` when the sources do not answer.
- The code then validates: no sources → no model call; the refusal word, no citations or a
  citation to a number that was not given → "insufficient evidence", with the reason.
- One LangChain `BaseChatModel` factory: fake (tests), Ollama Qwen3 8B (local), Bedrock
  Converse (staging). The agent reuses it.

## Alternatives considered

- **Dense only**: best numbers on this set, but no safety net for exact codes when the
  embedding model changes. Kept as a one-setting switch.
- **BM25** (ParadeDB `pg_search`) instead of `ts_rank_cd`: proper IDF, likely a better
  keyword list, but an extension the pgvector image and RDS do not ship.
- **Structured output (JSON with claim + source per item)**: stricter, but small local
  models break JSON more often than they omit a `[n]`; plain text + regex is enough here.
- **Trusting the model's citations**: rejected; a citation is only shown if it points to
  a source that was really in the prompt.

## Consequences

- Citation validation proves *where* an answer claims to come from, not that the claim is
  right: in the evaluation, Qwen3 8B cited the correct table and still misread a value
  range. Faithfulness needs a separate check (Ragas or an LLM judge, planned).
- `LLM_REASONING=true` (Qwen3 thinking) fixed that misreading at roughly 10x latency and
  tokens; it is off by default.
- Every retrieval change must be re-run through `make eval-retrieval` before it is kept.
