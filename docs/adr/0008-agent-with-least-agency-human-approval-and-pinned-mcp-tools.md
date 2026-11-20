# ADR-0008: Agent with least agency, human approval and pinned MCP tools

- Status: Accepted
- Date: 2026-11-20

## Context

The agent combines policy documents (RAG) with business records (orders,
invoices, tickets) and can lead to one real change: a return authorization (RMA). An
agent turns the model's output into actions, so the questions are less "how good is the
answer" and more "what is the worst thing it can do, and who decides". References: OWASP
Top 10 for LLM Applications 2025 (LLM01, LLM06), OWASP Top 10 for Agentic Applications
2026 (ASI01–ASI03, ASI09, ASI10), OWASP MCP Top 10 and the AWS Agentic AI Security
Scoping Matrix. Threats and controls are tracked in
[THREAT_MODEL.md](../security/THREAT_MODEL.md).

## Decision

1. **Least agency, Scope 2 ("prescribed agency")**: tools are read-only. The only write,
   `request_return_authorization`, is offered to the model as a tool but only creates a
   *pending action*; it executes after a supervisor approves it.
2. **An explicit LangGraph `StateGraph`** (`agent → tools → approval → agent`, plus
   `step_limit`) instead of a prebuilt ReAct agent: every edge, limit and pause is visible
   and testable. Limits: model calls per message, tool calls per turn, tool result size.
3. **Authorization in code, three layers**: the model is only offered the tools its role
   allows; every call is checked again before running (deny by default, role × tool);
   the database enforces the tenant (RLS). Identity comes from the request or the MCP
   launch configuration, never from tool arguments (no confused deputy).
4. **Approval rules**: approver role `supervisor`, never the requester (checked by the
   service, again inside the graph, and by a CHECK constraint on `return_authorizations`).
   A pending thread is claimed atomically, so a decision applies once. The approval request
   carries facts read from the database, not the model's summary. After approval the run
   resumes *as the requester*.
5. **Durable state**: LangGraph's PostgreSQL checkpointer in a separate schema
   (`agent_checkpoints`), reached only through a tenant-scoped `agent_threads` row.
6. **Append-only audit**: `audit_events` records tool calls (allowed, denied, error),
   proposals, decisions, executions and stops. The application role can INSERT and
   SELECT, not UPDATE or DELETE.
7. **MCP**: a read-only MCP server over stdio with one identity fixed at launch, sharing
   the same `execute_tool` path (authorize, validate, RLS, audit). The agent can use it
   (`AGENT_TOOL_TRANSPORT=mcp`); its client **pins** every tool by hashing name,
   description and input schema against the reviewed catalog and refuses unknown or
   changed tools (tool poisoning, rug pull).
8. **Kill switch**: `AGENT_ENABLED=false` makes the agent endpoints answer 503.

## Alternatives considered

- **Prebuilt ReAct agent** (`create_agent`): less code, but limits, approval and audit
  would be callbacks around a black box.
- **Let the model execute the RMA directly**: rejected; a prompt injection or a reading
  error becomes a real commitment of money.
- **`langchain-mcp-adapters`**: converts server tools into LangChain tools automatically,
  which is exactly what we do not want unverified. Our catalog stays the source of truth
  and the MCP listing is only accepted if it matches it.
- **MCP over HTTP**: needs OAuth 2.1 and per-user tokens; stdio has no network surface and
  fits a local, single-user client. Revisit if the server must be shared.
- **Checkpoint tables under RLS**: they have no tenant column (LangGraph's schema); the
  ownership check on `agent_threads` covers them instead (residual risk, documented).

## Consequences

- Measured with Qwen3 8B: without thinking, it proposed an RMA for an order delivered 40
  days earlier (the policy allows 30) and, after a stricter prompt, called the same order
  "within the window". The approval gate stopped the first; thinking mode
  (`AGENT_REASONING=true`, default) answered correctly and cited the late-return exception.
  A planted injection in ticket T-5009 was detected and not followed.
- The worst case of a manipulated model is a proposal a supervisor reviews with real data.
- MCP calls are audited without the agent's thread id (the server only knows its fixed
  identity); correlating them needs a run id passed through MCP metadata (later).
- Langfuse tracing is deferred: it needs an external account; the audit log and structured
  logs cover traceability for now.
