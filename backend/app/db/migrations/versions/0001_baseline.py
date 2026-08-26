"""Create the final SQLite baseline for local-first stages 1 and 2."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("current_content_version_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing','pending_review','reviewed','published','failed','deleted')",
            name="ck_knowledge_items_status",
        ),
    )
    op.create_index("ix_knowledge_items_status", "knowledge_items", ["status"])
    op.create_index("ix_knowledge_items_content_hash", "knowledge_items", ["content_hash"])
    op.create_index(
        "ix_active_item_hash", "knowledge_items", ["content_hash", "deleted_at"]
    )

    op.create_table(
        "source_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "knowledge_item_id",
            sa.String(36),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_source_artifacts_knowledge_item_id",
        "source_artifacts",
        ["knowledge_item_id"],
    )
    op.create_index(
        "ix_source_artifacts_content_hash", "source_artifacts", ["content_hash"]
    )

    op.create_table(
        "content_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "knowledge_item_id",
            sa.String(36),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("suggested_tags_json", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "knowledge_item_id", "version_no", name="uq_content_version_number"
        ),
    )
    op.create_index(
        "ix_content_versions_knowledge_item_id",
        "content_versions",
        ["knowledge_item_id"],
    )
    op.create_index(
        "ix_content_versions_content_hash", "content_versions", ["content_hash"]
    )

    op.create_table(
        "note_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "knowledge_item_id",
            sa.String(36),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("zhiliu_id", sa.String(36), nullable=False),
        sa.Column("relative_path", sa.String(500), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("last_written_hash", sa.String(64), nullable=True),
        sa.Column("sync_state", sa.String(32), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("knowledge_item_id", name="uq_note_binding_item"),
        sa.UniqueConstraint("zhiliu_id", name="uq_note_binding_zhiliu_id"),
        sa.UniqueConstraint("relative_path", name="uq_note_binding_path"),
        sa.CheckConstraint(
            "sync_state IN ('synced','changed','missing','conflict','error')",
            name="ck_note_bindings_sync_state",
        ),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "knowledge_item_id",
            sa.String(36),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "content_version_id",
            sa.String(36),
            sa.ForeignKey("content_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=True),
        sa.Column("embedding_version", sa.String(80), nullable=True),
        sa.Column("qdrant_point_id", sa.String(36), nullable=True, unique=True),
        sa.UniqueConstraint(
            "content_version_id", "ordinal", name="uq_chunk_version_ordinal"
        ),
    )
    op.create_index("ix_chunks_knowledge_item_id", "chunks", ["knowledge_item_id"])
    op.create_index("ix_chunks_content_version_id", "chunks", ["content_version_id"])

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(80), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True, unique=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_processing_jobs_state",
        ),
    )
    op.create_index("ix_processing_jobs_kind", "processing_jobs", ["kind"])
    op.create_index("ix_processing_jobs_state", "processing_jobs", ["state"])

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "processing_job_id",
            sa.String(36),
            sa.ForeignKey("processing_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(80), nullable=False),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "processing_job_id", "attempt_no", name="uq_job_attempt_number"
        ),
        sa.CheckConstraint(
            "state IN ('running','succeeded','failed','cancelled')",
            name="ck_job_attempts_state",
        ),
    )
    op.create_index(
        "ix_job_attempts_processing_job_id",
        "job_attempts",
        ["processing_job_id"],
    )

    op.create_table(
        "model_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "knowledge_item_id",
            sa.String(36),
            sa.ForeignKey("knowledge_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("operation", sa.String(50), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=True),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed')", name="ck_model_runs_status"
        ),
    )
    op.create_index("ix_model_runs_knowledge_item_id", "model_runs", ["knowledge_item_id"])

    op.execute(
        "CREATE VIRTUAL TABLE chunk_fts USING fts5("
        "chunk_id UNINDEXED, knowledge_item_id UNINDEXED, "
        "content_version_id UNINDEXED, content, source_locator UNINDEXED, "
        "tokenize='unicode61'"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chunk_fts")
    op.drop_table("model_runs")
    op.drop_table("job_attempts")
    op.drop_table("processing_jobs")
    op.drop_table("chunks")
    op.drop_table("note_bindings")
    op.drop_table("content_versions")
    op.drop_table("source_artifacts")
    op.drop_table("knowledge_items")
