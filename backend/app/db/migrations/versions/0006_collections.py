"""Add the minimal collection and collection-item relation used by MCP listing."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_collections"
down_revision: Union[str, Sequence[str], None] = "0005_workflow_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "collection_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "collection_id",
            sa.String(36),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "knowledge_item_id",
            sa.String(36),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "collection_id", "knowledge_item_id", name="uq_collection_item"
        ),
    )
    op.create_index(
        "ix_collection_items_collection_id", "collection_items", ["collection_id"]
    )
    op.create_index(
        "ix_collection_items_knowledge_item_id",
        "collection_items",
        ["knowledge_item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_collection_items_knowledge_item_id", table_name="collection_items")
    op.drop_index("ix_collection_items_collection_id", table_name="collection_items")
    op.drop_table("collection_items")
    op.drop_table("collections")
