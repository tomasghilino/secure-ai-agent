# ADR-0009: Verified identity from JWTs and abuse limits in PostgreSQL

- Status: Accepted
- Date: 2026-11-22

## Context

Until now the API took the tenant, user and role from `X-Tenant-ID`, `X-User-ID` and
`X-User-Role` headers, accepted only in local and test. Anyone who can reach a deployed
API could send those headers (STRIDE spoofing, OWASP API2). Every security control
downstream (Row-Level Security, the role-by-tool matrix, separation of duties, the audit
log) is only as good as the identity it receives.

Model calls cost money per token. Without limits, one user or one compromised client can
exhaust the provider budget or degrade service for every tenant (OWASP LLM10 "unbounded
consumption", API4).

## Decision

1. **JWT bearer tokens** from an OpenID Connect provider (Amazon Cognito in staging; a
   local issuer in development, `make token`). The verifier:
   - accepts only asymmetric algorithms, configured as an allowlist; `none` and HMAC are
     rejected at startup, which rules out algorithm confusion;
   - selects the key by `kid` from the provider's JWKS (cached, refreshed on rotation) and
     never tries every key;
   - checks the signature, `exp`, `nbf`, `iat`, the exact `iss` and `aud`, with 30 s of
     clock skew;
   - maps claims to the principal: `sub` is the user, a configurable tenant claim and
     exactly **one** known role (Cognito: `custom:tenant_id`, `cognito:groups`). A token
     with several roles is refused instead of silently picking the strongest.
   Failures return a generic 401 with `WWW-Authenticate: Bearer`; the reason is logged,
   not returned. A valid token without a tenant or a role is a 403.
2. **Identity headers only where they are harmless**: `AUTH_MODE=dev_headers` is refused at
   startup outside local and test, and with JWT on the headers are ignored.
3. **Roles from claims gate the knowledge base too**: a new `editor` role is the only one
   allowed to upload documents (OWASP LLM04, data poisoning). Supervisors approve returns
   but cannot rewrite the returns policy.
4. **Rate limit per user and token budget per tenant**, as fixed-window counters in a
   `usage_counters` table, incremented with one atomic upsert
   (`INSERT … ON CONFLICT DO UPDATE … RETURNING`) under Row-Level Security:
   - requests per user per minute on every `/v1` route (429 with `Retry-After`);
   - model tokens per tenant per UTC day, checked before a model-backed request and charged
     afterwards with the tokens the provider reported. A LangChain callback adds up every
     model call of a run, including the agent's calls inside the graph.
5. **HTTP hardening**: request IDs, security headers (no sniffing, no framing, no caching,
   `default-src 'none'`), HSTS in staging, no OpenAPI schema in staging, unhandled errors
   as a generic 500 with the request ID, and validation errors without the submitted values.

## Alternatives considered

- **API keys per client**: no user identity, so no separation of duties or per-user audit.
- **Trusting an upstream gateway's headers**: valid behind API Gateway with a JWT
  authorizer, but the API would then depend on network placement for its security; the
  verifier keeps it correct on its own and costs microseconds per request.
- **Redis or an API gateway for limits**: one more service to run and secure. PostgreSQL
  is already the source of truth, and atomic upserts give correct limits across replicas
  at this traffic level. A gateway rate limit (AWS WAF) is still useful in front.
- **Sliding windows or token buckets**: smoother, but more state; a fixed window allows
  bursts of up to twice the limit at a window boundary, which is acceptable here.
- **Hard per-request token reservation**: precise but needs an estimate before the call;
  the budget is checked before and charged after, and the overrun is bounded by
  `AGENT_MAX_STEPS` and `LLM_MAX_OUTPUT_TOKENS`.

## Consequences

- Tenant, user and role reach RLS, the tool matrix and the audit log only from a verified
  signature. Tests cover forged signatures, unknown `kid`, `alg=none`, HMAC with the public
  key, a tampered role, wrong issuer or audience, expiry and ambiguous roles.
- A request costs one extra upsert (rate limit), two for model-backed endpoints. Old
  counter windows are history and can be pruned by a scheduled job.
- An unknown `kid` triggers a JWKS refetch; a flood of random `kid` values would hit the
  provider's endpoint. The gateway rate limit and the JWKS cache bound it.
