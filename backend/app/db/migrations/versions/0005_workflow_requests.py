"""Add durable request identity for graph-backed RAG idempotency."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_workflow_requests"
down_revision: Union[str, Sequence[str], None] = "0004_video_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "model_run_id",
            sa.String(36),
            sa.ForeignKey("model_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('rag_answer')",
            name="ck_workflow_requests_operation",
        ),
        sa.CheckConstraint(
            "status IN ('running','succeeded','refused','failed')",
            name="ck_workflow_requests_status",
        ),
    )
    op.create_index(
        "ix_workflow_requests_status", "workflow_requests", ["status"]
    )
    op.create_index(
        "ix_workflow_requests_model_run_id", "workflow_requests", ["model_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_requests_model_run_id", table_name="workflow_requests")
    op.drop_index("ix_workflow_requests_status", table_name="workflow_requests")
    op.drop_table("workflow_requests")
