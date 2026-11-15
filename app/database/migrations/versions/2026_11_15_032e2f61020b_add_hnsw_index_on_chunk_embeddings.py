"""add hnsw index on chunk embeddings

Revision ID: 032e2f61020b
Revises: c3fcea3371eb
Create Date: 2026-11-15 20:35:50.093933
"""

from collections.abc import Sequence

from alembic import op

revision: str = "032e2f61020b"
down_revision: str | Sequence[str] | None = "c3fcea3371eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain CREATE INDEX blocks writes to `chunks` while the graph is built: instant for
    # hundreds of rows. On a large live table use CREATE INDEX CONCURRENTLY instead (it
    # cannot run inside a transaction: wrap it in op.get_context().autocommit_block()).
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
