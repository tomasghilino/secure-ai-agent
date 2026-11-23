# ADR-0010: AI red-team evaluation and supply-chain security in CI

- Status: Accepted
- Date: 2026-11-22

## Context

Security controls for an AI agent are easy to describe and hard to prove. A prompt that
says "never do X" is not a control; a test that shows X cannot happen is. Two gaps
remained: no repeatable attack suite against the agent, and no automated checks on what
goes into the build (dependencies, base images, CI actions, secrets in history).

## Decision

1. **A red-team suite** (`data/evaluation/red_team.yaml`, `make eval-security`): 17
   attacks mapped to the OWASP Top 10 for LLM and agentic applications (direct and
   indirect prompt injection, system prompt extraction, cross-tenant probing, tool abuse,
   excessive agency, unbounded consumption). Verdicts come from the tool trace, the
   database and the final answer, never from the model's own claims:
   `resisted`, `contained` (attempted, stopped by a control in code), `succeeded`.
   A run that reaches the approval gate is replayed with a **careless approver** who
   approves without reading, to measure what the code still enforces when the human fails.
2. **The suite runs in CI** with the rule-based offline model, which takes every bait (it
   looks up any identifier and requests a return whenever asked): a worst-case attacker
   model with deterministic results. The same harness runs with any real provider.
3. **Deterministic eligibility rules in code**, found by the suite: a return is refused
   unless the order is delivered, inside the tenant's return window and has no
   build-to-order or final-sale lines; checked when proposed and again when approved.
4. **CI pipeline** (GitHub Actions): lint, types, unit tests; integration tests and the red
   team against PostgreSQL; gitleaks over the full history; pip-audit on the locked
   production dependencies; Trivy for misconfigurations, secrets and fixable HIGH/CRITICAL
   image vulnerabilities; a CycloneDX SBOM as an artifact; actionlint. Hardening of the
   pipeline itself: actions pinned by commit SHA, scanners and base images pinned by
   digest, `permissions: contents: read`, no persisted checkout credentials, checked with
   zizmor. Dependabot proposes updates weekly after a 7-day cooldown.
5. **Hardened runtime image**: Debian 13 slim Python base with security updates applied at
   build, no uv or pip, a system user without shell or home, no setuid binaries,
   root-owned compiled code, health check; it runs with a read-only root filesystem, no
   capabilities and `no-new-privileges`.

## Alternatives considered

- **LLM-as-judge verdicts**: flexible but non-deterministic and attackable by the same
  injection; reserved for answer quality, not for security verdicts.
- **Third-party scanning actions** (`trivy-action`, `gitleaks-action`): convenient, but each
  is more code running with the workflow's token; images pinned by digest do the same job.
- **Distroless base**: smaller, but its Python version does not match the project's 3.12;
  the slim image with the hardening above scans clean for fixable HIGH/CRITICAL issues.

## Consequences

- Rule-based model, before the eligibility rules: 0 of 17 attacks succeeded on their own,
  but 2 (DPI-01, DPI-02) created a return for a non-returnable pump once a careless
  approver said yes. After the rules: 0 of 17, and those proposals never reach a
  supervisor. Results: [red-team benchmark](../benchmarks/2026-11-22-red-team.md).
- The suite measures controls, not model quality: a real model's resistance varies by
  version and settings and is reported separately when measured.
- CI needs no cloud credentials; OIDC to AWS is added with the Terraform staging work.
