"""Add confirmed tags and AI collection suggestions to the review model."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_tags_and_review_suggestions"
down_revision: Union[str, Sequence[str], None] = "0006_collections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "content_versions",
        sa.Column(
            "suggested_collections_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("normalized_name", sa.String(80), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "knowledge_item_tags",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "knowledge_item_id",
            sa.String(36),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            sa.String(36),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "knowledge_item_id", "tag_id", name="uq_knowledge_item_tag"
        ),
    )
    op.create_index(
        "ix_knowledge_item_tags_knowledge_item_id",
        "knowledge_item_tags",
        ["knowledge_item_id"],
    )
    op.create_index(
        "ix_knowledge_item_tags_tag_id",
        "knowledge_item_tags",
        ["tag_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_item_tags_tag_id", table_name="knowledge_item_tags")
    op.drop_index(
        "ix_knowledge_item_tags_knowledge_item_id",
        table_name="knowledge_item_tags",
    )
    op.drop_table("knowledge_item_tags")
    op.drop_table("tags")
    op.drop_column("content_versions", "suggested_collections_json")
