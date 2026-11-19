"""Database tables, declared with SQLAlchemy 2.

These classes are the source of truth for the schema. Alembic compares them with the live
database to draft a migration (``make migration m="..."``); every draft is reviewed by
hand before it is applied with ``make migrate``.

Design notes:

- Document facts (tenant, version, status) live in ``documents``, once per document
  version. Superseding a version updates one row, not every chunk.
- ``chunks.tenant_id`` is a deliberate copy: a document never changes tenant, so the copy
  cannot go stale, and the chunks table can enforce tenant isolation on its own (RLS).
- ``status`` is text + CHECK instead of a PostgreSQL ENUM: adding a value to a CHECK is a
  simple migration, while ENUM changes are awkward (values cannot be removed).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Changing this needs a migration AND re-embedding every chunk (the vectors of different
# models or sizes are not comparable). Must match EMBEDDING_DIMENSIONS in the settings.
EMBEDDING_DIMENSIONS = 1024

# Predictable constraint names, so later migrations can alter or drop them by name.
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(column_0_label)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Plain `str` columns become PostgreSQL TEXT: no arbitrary VARCHAR length limits.
    type_annotation_map: ClassVar[dict[Any, Any]] = {str: Text}


class DocumentRecord(Base):
    """One version of one registered document, e.g. NSI-POL-RET-003 v3.0."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "doc_key", "version"),  # a version is registered once
        UniqueConstraint("tenant_id", "sha256"),  # the same file is ingested once
        CheckConstraint("status IN ('draft', 'active', 'superseded')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str]
    # Registry ID shared by every version ("doc_id" in the manifest; renamed here so it is
    # not confused with chunks.document_id, which points to one version).
    doc_key: Mapped[str]
    version: Mapped[str]
    title: Mapped[str]
    status: Mapped[str]
    effective_date: Mapped[date]
    classification: Mapped[str]
    sha256: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChunkRecord(Base):
    """One chunk of a document version, with the embedding of its text."""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        # Approximate nearest-neighbour index for `ORDER BY embedding <=> :query`. The
        # operator class must match the operator: vector_cosine_ops serves `<=>` only.
        # m / ef_construction are pgvector's defaults, written out to be visible.
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")  # deleting a version deletes its chunks
    )
    tenant_id: Mapped[str]
    chunk_index: Mapped[int]
    page_number: Mapped[int]
    section_path: Mapped[list[str]] = mapped_column(ARRAY(Text))
    content: Mapped[str]  # heading path + body: exactly the text that was embedded
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str]  # which model produced the vector


class IngestionJobRecord(Base):
    """One uploaded PDF waiting for, or processed by, the ingestion worker.

    The API creates it (``queued``) and the worker moves it to ``running`` and then to
    ``succeeded`` or ``failed``. Until the worker creates the document row, the job
    carries the version's registry metadata.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name="status"),
        CheckConstraint(
            "document_status IN ('draft', 'active', 'superseded')", name="document_status"
        ),
        # The upload looks for a job of the same file before creating another one.
        Index("ix_ingestion_jobs_tenant_id_sha256", "tenant_id", "sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str]
    status: Mapped[str]
    attempts: Mapped[int]  # how many times a worker started it
    error: Mapped[str | None]  # why it failed (last attempt)
    sha256: Mapped[str]
    storage_key: Mapped[str]  # where the uploaded PDF is stored
    # Registry metadata of the uploaded version (becomes the documents row).
    doc_key: Mapped[str]
    version: Mapped[str]
    title: Mapped[str]
    document_status: Mapped[str]
    effective_date: Mapped[date]
    classification: Mapped[str]
    # Set when the job succeeds.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
