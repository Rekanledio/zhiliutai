from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing','pending_review','reviewed','published','failed','deleted')",
            name="ck_knowledge_items_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="processing", index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    current_content_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceArtifact(Base):
    __tablename__ = "source_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_item_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE"), index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(32))
    media_type: Mapped[str] = mapped_column(String(100))
    relative_path: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    source_locator: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContentVersion(Base):
    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint("knowledge_item_id", "version_no", name="uq_content_version_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_item_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_tags_json: Mapped[str] = mapped_column(Text, default="[]")
    prompt_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class NoteBinding(Base):
    __tablename__ = "note_bindings"
    __table_args__ = (
        UniqueConstraint("knowledge_item_id", name="uq_note_binding_item"),
        UniqueConstraint("zhiliu_id", name="uq_note_binding_zhiliu_id"),
        UniqueConstraint("relative_path", name="uq_note_binding_path"),
        CheckConstraint(
            "sync_state IN ('synced','changed','missing','conflict','error')",
            name="ck_note_bindings_sync_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_item_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE")
    )
    zhiliu_id: Mapped[str] = mapped_column(String(36))
    relative_path: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64))
    last_written_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sync_state: Mapped[str] = mapped_column(String(32), default="synced")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("content_version_id", "ordinal", name="uq_chunk_version_ordinal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_item_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="CASCADE"), index=True
    )
    content_version_id: Mapped[str] = mapped_column(
        ForeignKey("content_versions.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(32))
    source_locator: Mapped[str] = mapped_column(Text)
    embedding_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    qdrant_point_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_processing_jobs_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    state: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(80), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("processing_job_id", "attempt_no", name="uq_job_attempt_number"),
        CheckConstraint(
            "state IN ('running','succeeded','failed','cancelled')",
            name="ck_job_attempts_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    processing_job_id: Mapped[str] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="CASCADE"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="running")
    stage: Mapped[str] = mapped_column(String(80), default="starting")
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelRun(Base):
    __tablename__ = "model_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed')", name="ck_model_runs_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    operation: Mapped[str] = mapped_column(String(50))
    prompt_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parameters_json: Mapped[str] = mapped_column(Text, default="{}")
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


Index("ix_active_item_hash", KnowledgeItem.content_hash, KnowledgeItem.deleted_at)
