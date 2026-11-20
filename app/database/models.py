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
- Every table with a ``tenant_id`` column is protected by Row-Level Security (see
  ``app/database/tenancy.py``). A new tenant table needs its grant and policy in the same
  migration; a test fails otherwise.
- Business records have readable natural keys (``C-1001``, ``SO-2026-0101``) next to the
  UUID: people and tools refer to records by number, never by UUID.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
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
        # At most one active version per document: a partial unique index (only the rows
        # matching WHERE are indexed). Ingesting a newer active version supersedes the old one.
        Index(
            "uq_documents_one_active_version",
            "tenant_id",
            "doc_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
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
        # Inverted index for full-text search: lexeme -> chunks that contain it.
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
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
    # Full-text representation, computed by PostgreSQL from `content` on every write
    # (never set by the application). English stemming: "returns" and "returned" -> "return".
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
    )


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


class EmbeddingCacheRecord(Base):
    """A vector already computed for a text, so unchanged text is never embedded twice.

    Keyed by tenant: sharing vectors across tenants would let one tenant infer, from a
    cache hit, that another tenant has the exact same text.
    """

    __tablename__ = "embedding_cache"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "model_id", "dimensions", "content_sha256"),
    )

    tenant_id: Mapped[str]
    model_id: Mapped[str]
    dimensions: Mapped[int]
    content_sha256: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------- business data
# Read by the agent's tools. Seeded from data/source/business with `make seed`.

Money = Numeric(12, 2)  # exact decimal amounts, never float


class ProductRecord(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("tenant_id", "sku"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str]
    sku: Mapped[str]
    name: Mapped[str]
    category: Mapped[str]
    specs: Mapped[str]
    list_price: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str]
    warranty_months: Mapped[int]
    lead_time_days: Mapped[int]
    returnable: Mapped[bool]


class CustomerRecord(Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "customer_number"),
        CheckConstraint("support_tier IN ('standard', 'premium', 'platinum')", name="support_tier"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str]
    customer_number: Mapped[str]
    name: Mapped[str]
    segment: Mapped[str]
    country: Mapped[str]
    support_tier: Mapped[str]  # sets the SLA targets (NSI-SLA-CS-002)


class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "order_number"),
        CheckConstraint(
            "status IN ('confirmed', 'shipped', 'delivered', 'cancelled')", name="status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str]
    order_number: Mapped[str]
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str]
    order_date: Mapped[date]
    delivered_date: Mapped[date | None]  # starts the return window and the warranty
    currency: Mapped[str]
    total_amount: Mapped[Decimal] = mapped_column(Money)


class OrderLineRecord(Base):
    __tablename__ = "order_lines"
    __table_args__ = (UniqueConstraint("order_id", "line_number"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str]
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    line_number: Mapped[int]
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]
    unit_price: Mapped[Decimal] = mapped_column(Money)
    final_sale: Mapped[bool]  # clearance: not returnable (NSI-POL-RET-003 §7)


class InvoiceRecord(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "invoice_number"),
        # "Overdue" is not a status: it is open + past due_date, computed when asked.
        CheckConstraint("status IN ('open', 'paid', 'void')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str]
    invoice_number: Mapped[str]
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    issue_date: Mapped[date]
    due_date: Mapped[date]
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str]
    status: Mapped[str]
    paid_date: Mapped[date | None]


class SupportTicketRecord(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "ticket_number"),
        CheckConstraint("priority IN ('P1', 'P2', 'P3', 'P4')", name="priority"),
        CheckConstraint("status IN ('open', 'in_progress', 'resolved', 'closed')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str]
    ticket_number: Mapped[str]
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("products.id"))
    priority: Mapped[str]
    status: Mapped[str]
    subject: Mapped[str]
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupplierRecord(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "supplier_code"),
        CheckConstraint("status IN ('approved', 'pending', 'suspended')", name="status"),
        CheckConstraint("risk_rating IN ('low', 'medium', 'high')", name="risk_rating"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str]
    supplier_code: Mapped[str]
    name: Mapped[str]
    category: Mapped[str]
    country: Mapped[str]
    status: Mapped[str]
    risk_rating: Mapped[str]
    annual_spend: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str]
    contract_doc_key: Mapped[str | None]  # e.g. NSI-AGR-SUP-014


# --------------------------------------------------------------------------- agent


class AgentThreadRecord(Base):
    """One agent conversation: who started it and where it stands.

    The conversation itself (messages, tool results) lives in LangGraph's checkpoint
    tables, in the separate ``agent_checkpoints`` schema. This row is the tenant-scoped
    handle to it: the API checks ownership here (RLS) before touching the checkpoint.
    """

    __tablename__ = "agent_threads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'awaiting_approval', 'completed', 'stopped', 'failed')",
            name="status",
        ),
        Index("ix_agent_threads_tenant_id_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # = LangGraph thread_id
    tenant_id: Mapped[str]
    # The agent always acts as this identity, also after a supervisor approves an action.
    created_by: Mapped[str]
    created_by_role: Mapped[str]
    title: Mapped[str]  # first message, truncated
    status: Mapped[str]
    # The action waiting for a decision, with the facts the approver needs.
    pending_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditEventRecord(Base):
    """Append-only record of what the agent and its users did.

    The application role may INSERT and SELECT but not UPDATE or DELETE (see the
    migration): a compromised request cannot rewrite history. Arguments are stored,
    truncated; tool *results* never are (they hold business data).
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('tool_call', 'action_proposed', 'action_decided', "
            "'action_executed', 'run_stopped')",
            name="event_type",
        ),
        Index("ix_audit_events_tenant_id_occurred_at", "tenant_id", "occurred_at"),
        Index("ix_audit_events_thread_id", "thread_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str]
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor_id: Mapped[str]
    actor_role: Mapped[str]
    thread_id: Mapped[uuid.UUID | None]
    event_type: Mapped[str]
    tool_name: Mapped[str | None]
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    outcome: Mapped[str]  # ok | denied | error | approved | rejected | proposed | ...
    detail: Mapped[str | None]


class ReturnAuthorizationRecord(Base):
    """A return authorization (RMA): the only write the agent can lead to, after approval."""

    __tablename__ = "return_authorizations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "rma_number"),
        UniqueConstraint("tenant_id", "order_id"),  # one RMA per order: approvals are idempotent
        # Separation of duties, enforced by the database too: nobody approves their own request.
        CheckConstraint("approved_by <> requested_by", name="approver_is_not_requester"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str]
    rma_number: Mapped[str]
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    reason: Mapped[str]
    requested_by: Mapped[str]
    approved_by: Mapped[str]
    thread_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
