"""add full-text search column and GIN index on chunks

Revision ID: 4815017b7da7
Revises: 0b0105a0d6f7
Create Date: 2026-11-19 15:51:55.200575
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4815017b7da7"
down_revision: str | Sequence[str] | None = "0b0105a0d6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A STORED generated column: PostgreSQL computes it for the existing rows now (a table
    # rewrite, fine for a small table; a big one would need a maintenance window or a
    # batched backfill) and keeps it in sync on every insert or update.
    op.add_column(
        "chunks",
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], unique=False, postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_chunks_tsv", table_name="chunks", postgresql_using="gin")
    op.drop_column("chunks", "tsv")
