"""Store structured source metadata and locator segments on content versions."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_source_metadata"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "content_versions",
        sa.Column("source_metadata_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("content_versions", "source_metadata_json")
