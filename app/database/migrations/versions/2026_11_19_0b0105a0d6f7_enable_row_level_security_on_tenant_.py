"""enable row level security on tenant tables

Revision ID: 0b0105a0d6f7
Revises: ae8c9ce80c85
Create Date: 2026-11-19 15:40:12.000000

Second layer of tenant isolation (the first is the tenant filter in every query). Tenant
requests run as the role ``northstar_app`` with ``app.tenant_id`` set for the transaction
(see app/database/tenancy.py). For that role, PostgreSQL only shows and accepts rows whose
``tenant_id`` matches, even if a query forgets its WHERE. The table owner, used by
migrations and admin scripts, is not subject to these policies.

Written by hand: autogenerate does not see roles, grants or policies.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0b0105a0d6f7"
down_revision: str | Sequence[str] | None = "ae8c9ce80c85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "northstar_app"
TENANT_TABLES = (
    "documents",
    "chunks",
    "ingestion_jobs",
    "embedding_cache",
    "products",
    "customers",
    "orders",
    "order_lines",
    "invoices",
    "support_tickets",
    "suppliers",
)
# current_setting(..., true) returns NULL instead of failing when the setting is missing,
# and `tenant_id = NULL` is never true: no tenant set means no rows (fail closed).
TENANT_MATCHES = "tenant_id = current_setting('app.tenant_id', true)"


def upgrade() -> None:
    # Roles belong to the whole PostgreSQL server, not to one database (the test database
    # shares it), so it is created only if missing. NOLOGIN: nobody connects as this role;
    # the application switches to it per transaction (SET LOCAL ROLE).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    for table in TENANT_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # USING filters the rows a statement can see; WITH CHECK rejects writing a row
        # for another tenant.
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({TENANT_MATCHES}) WITH CHECK ({TENANT_MATCHES})"
        )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    # The role is left in place: other databases on the same server may still use it.
