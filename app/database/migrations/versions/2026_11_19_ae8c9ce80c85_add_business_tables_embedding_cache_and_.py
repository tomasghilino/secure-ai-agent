"""add business tables, embedding cache and one active version per document

Revision ID: ae8c9ce80c85
Revises: 39cb7f1cf0aa
Create Date: 2026-11-19 15:38:00.962660
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "ae8c9ce80c85"
down_revision: str | Sequence[str] | None = "39cb7f1cf0aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_number", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("segment", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("support_tier", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "support_tier IN ('standard', 'premium', 'platinum')",
            name=op.f("ck_customers_support_tier"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint(
            "tenant_id", "customer_number", name=op.f("uq_customers_tenant_id_customer_number")
        ),
    )
    op.create_table(
        "embedding_cache",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "model_id", "dimensions", "content_sha256", name=op.f("pk_embedding_cache")
        ),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("specs", sa.Text(), nullable=False),
        sa.Column("list_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("warranty_months", sa.Integer(), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), nullable=False),
        sa.Column("returnable", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("tenant_id", "sku", name=op.f("uq_products_tenant_id_sku")),
    )
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("supplier_code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("risk_rating", sa.Text(), nullable=False),
        sa.Column("annual_spend", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("contract_doc_key", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "risk_rating IN ('low', 'medium', 'high')", name=op.f("ck_suppliers_risk_rating")
        ),
        sa.CheckConstraint(
            "status IN ('approved', 'pending', 'suspended')", name=op.f("ck_suppliers_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppliers")),
        sa.UniqueConstraint(
            "tenant_id", "supplier_code", name=op.f("uq_suppliers_tenant_id_supplier_code")
        ),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("order_number", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("delivered_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("total_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint(
            "status IN ('confirmed', 'shipped', 'delivered', 'cancelled')",
            name=op.f("ck_orders_status"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_orders_customer_id_customers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint(
            "tenant_id", "order_number", name=op.f("uq_orders_tenant_id_order_number")
        ),
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("invoice_number", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("paid_date", sa.Date(), nullable=True),
        sa.CheckConstraint("status IN ('open', 'paid', 'void')", name=op.f("ck_invoices_status")),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_invoices_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_invoices_order_id_orders")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint(
            "tenant_id", "invoice_number", name=op.f("uq_invoices_tenant_id_invoice_number")
        ),
    )
    op.create_table(
        "order_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("final_sale", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_lines_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_order_lines_product_id_products")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_lines")),
        sa.UniqueConstraint(
            "order_id", "line_number", name=op.f("uq_order_lines_order_id_line_number")
        ),
    )
    op.create_table(
        "support_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("ticket_number", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "priority IN ('P1', 'P2', 'P3', 'P4')", name=op.f("ck_support_tickets_priority")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'resolved', 'closed')",
            name=op.f("ck_support_tickets_status"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_support_tickets_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_support_tickets_order_id_orders")
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], name=op.f("fk_support_tickets_product_id_products")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_tickets")),
        sa.UniqueConstraint(
            "tenant_id", "ticket_number", name=op.f("uq_support_tickets_tenant_id_ticket_number")
        ),
    )
    op.create_index(
        "uq_documents_one_active_version",
        "documents",
        ["tenant_id", "doc_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_documents_one_active_version",
        table_name="documents",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("support_tickets")
    op.drop_table("order_lines")
    op.drop_table("invoices")
    op.drop_table("orders")
    op.drop_table("suppliers")
    op.drop_table("products")
    op.drop_table("embedding_cache")
    op.drop_table("customers")
