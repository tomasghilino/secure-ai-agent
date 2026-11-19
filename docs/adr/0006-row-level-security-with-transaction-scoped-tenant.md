# ADR-0006: Row-Level Security with a transaction-scoped tenant

- Status: Accepted
- Date: 2026-11-19

## Context

Every tenant-scoped query filters by `tenant_id`. One forgotten filter (in an endpoint, a
tool, a hand-written SQL query) would leak another tenant's data, and an agent with tools
multiplies the number of queries. The architecture asks for a second, independent layer
inside the database.

PostgreSQL Row-Level Security (RLS) filters rows by policy, but it does not apply to
superusers or to the table owner. The local `northstar` user is both, and the application
cannot use a different connection pool per tenant.

## Decision

1. A migration creates a `NOLOGIN` role, `northstar_app`, grants it DML on every tenant
   table, enables RLS and adds one policy per table:
   `tenant_id = current_setting('app.tenant_id', true)` for both reading (`USING`) and
   writing (`WITH CHECK`).
2. Tenant work runs in *tenant sessions* (`app/database/tenancy.py`). An event hook runs,
   at the start of every transaction:
   `SET LOCAL ROLE northstar_app` and `set_config('app.tenant_id', <tenant>, true)`.
   Both end with the transaction, so a pooled connection never keeps a tenant.
3. API requests always get a tenant session (the session dependency depends on the tenant
   dependency). The worker opens one per job, with the tenant carried in the queue message.
4. Migrations, seeding, admin scripts and the job sweeper use plain sessions as the owner.
5. A test fails if any table with a `tenant_id` column lacks RLS or a policy.

## Alternatives considered

- **Only application filters**: one bug away from a leak; no defense in depth.
- **Separate login role for the app** (its own connection URL): also valid, and what a
  managed database would use. It needs a second credential locally; `SET LOCAL ROLE` keeps
  one connection URL and works the same way (the login user only needs membership of the
  role in staging).
- **`FORCE ROW LEVEL SECURITY` on the owner**: does not help, because the local user is a
  superuser, and it would also restrict migrations and admin tasks.
- **Schema or database per tenant**: strong isolation, but many schemas to migrate and no
  shared HNSW index; overkill for two tenants.

## Consequences

- Fail closed: with the role set but no tenant, `current_setting` returns NULL, the policy
  is never true, and no rows are visible.
- Code that runs as the owner (sweeper, scripts) is not protected by RLS and must keep its
  own filters; it is deliberately small.
- Every new tenant table needs its grant and policy in the same migration (guarded by the
  test above).
