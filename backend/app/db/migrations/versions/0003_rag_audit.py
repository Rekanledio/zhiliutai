"""Add RAG answer audit fields and citation snapshots."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_rag_audit"
down_revision: Union[str, Sequence[str], None] = "0002_source_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_runs", sa.Column("input_json", sa.Text(), nullable=True))
    op.add_column("model_runs", sa.Column("output_json", sa.Text(), nullable=True))
    op.create_table(
        "citations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "model_run_id",
            sa.String(36),
            sa.ForeignKey("model_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            sa.String(36),
            sa.ForeignKey("chunks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("knowledge_item_id", sa.String(36), nullable=False),
        sa.Column("content_version_id", sa.String(36), nullable=False),
        sa.Column("label", sa.String(20), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("chunk_content_hash", sa.String(64), nullable=False),
        sa.Column("source_locator", sa.Text(), nullable=False),
        sa.Column("retrieval_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("model_run_id", "label", name="uq_citation_model_run_label"),
    )
    op.create_index("ix_citations_model_run_id", "citations", ["model_run_id"])
    op.create_index("ix_citations_chunk_id", "citations", ["chunk_id"])
    op.create_index("ix_citations_knowledge_item_id", "citations", ["knowledge_item_id"])
    op.create_index(
        "ix_citations_content_version_id", "citations", ["content_version_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_citations_content_version_id", table_name="citations")
    op.drop_index("ix_citations_knowledge_item_id", table_name="citations")
    op.drop_index("ix_citations_chunk_id", table_name="citations")
    op.drop_index("ix_citations_model_run_id", table_name="citations")
    op.drop_table("citations")
    op.drop_column("model_runs", "output_json")
    op.drop_column("model_runs", "input_json")
