"""Add video artifact lifecycle fields and pending content version state."""

from typing import Sequence, Union

from alembic import op


revision: str = "0004_video_lifecycle"
down_revision: Union[str, Sequence[str], None] = "0003_rag_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The final architecture is SQLite-only.  Native ADD COLUMN keeps the
    # existing child rows intact; batch-recreating knowledge_items would
    # trigger its ON DELETE CASCADE relationships while foreign_keys is on.
    op.execute(
        "ALTER TABLE source_artifacts ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE source_artifacts ADD COLUMN retention_policy VARCHAR(32) NOT NULL "
        "DEFAULT 'permanent' CONSTRAINT ck_source_artifacts_retention_policy CHECK "
        "(retention_policy IN ('permanent','until_expiry','delete_after_processing'))"
    )
    op.execute(
        "ALTER TABLE source_artifacts ADD COLUMN retention_expires_at DATETIME"
        " CONSTRAINT ck_source_artifacts_until_expiry_requires_expiration CHECK "
        "(retention_policy != 'until_expiry' OR retention_expires_at IS NOT NULL)"
    )
    op.execute(
        "ALTER TABLE source_artifacts ADD COLUMN cleanup_state VARCHAR(20) NOT NULL "
        "DEFAULT 'not_due' CONSTRAINT ck_source_artifacts_cleanup_state CHECK "
        "(cleanup_state IN ('not_due','due','deleted','failed'))"
    )
    op.execute(
        "ALTER TABLE source_artifacts ADD COLUMN cleaned_at DATETIME "
        "CONSTRAINT ck_source_artifacts_deleted_at_state CHECK "
        "((cleanup_state = 'deleted') = (cleaned_at IS NOT NULL))"
    )
    op.create_index(
        "ix_source_artifacts_cleanup_due",
        "source_artifacts",
        ["cleanup_state", "retention_expires_at"],
    )

    op.execute(
        "ALTER TABLE knowledge_items ADD COLUMN pending_content_version_id VARCHAR(36) "
        "CONSTRAINT fk_knowledge_items_pending_content_version "
        "REFERENCES content_versions(id) ON DELETE SET NULL "
        "CONSTRAINT ck_knowledge_items_pending_version_distinct CHECK "
        "(pending_content_version_id IS NULL OR "
        "pending_content_version_id != current_content_version_id)"
    )
    op.create_index(
        "ix_knowledge_items_pending_content_version_id",
        "knowledge_items",
        ["pending_content_version_id"],
    )
    op.execute(
        """
        CREATE TRIGGER trg_knowledge_items_pending_version_owner_insert
        BEFORE INSERT ON knowledge_items
        FOR EACH ROW
        WHEN NEW.pending_content_version_id IS NOT NULL
         AND NOT EXISTS (
             SELECT 1
             FROM content_versions
             WHERE content_versions.id = NEW.pending_content_version_id
               AND content_versions.knowledge_item_id = NEW.id
         )
        BEGIN
            SELECT RAISE(
                ABORT,
                'pending content version must belong to knowledge item'
            );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_knowledge_items_pending_version_owner_update
        BEFORE UPDATE OF pending_content_version_id, id ON knowledge_items
        FOR EACH ROW
        WHEN NEW.pending_content_version_id IS NOT NULL
         AND NOT EXISTS (
             SELECT 1
             FROM content_versions
             WHERE content_versions.id = NEW.pending_content_version_id
               AND content_versions.knowledge_item_id = NEW.id
         )
        BEGIN
            SELECT RAISE(
                ABORT,
                'pending content version must belong to knowledge item'
            );
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_content_versions_pending_version_owner_update
        BEFORE UPDATE OF knowledge_item_id, id ON content_versions
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1
            FROM knowledge_items
            WHERE knowledge_items.pending_content_version_id = OLD.id
              AND (
                  NEW.id != OLD.id
                  OR NEW.knowledge_item_id != knowledge_items.id
              )
        )
        BEGIN
            SELECT RAISE(
                ABORT,
                'pending content version ownership cannot change'
            );
        END
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_content_versions_pending_version_owner_update"
    )
    op.execute(
        "DROP TRIGGER trg_knowledge_items_pending_version_owner_update"
    )
    op.execute(
        "DROP TRIGGER trg_knowledge_items_pending_version_owner_insert"
    )
    op.drop_index(
        "ix_knowledge_items_pending_content_version_id", table_name="knowledge_items"
    )
    op.execute("ALTER TABLE knowledge_items DROP COLUMN pending_content_version_id")

    op.drop_index("ix_source_artifacts_cleanup_due", table_name="source_artifacts")
    op.execute("ALTER TABLE source_artifacts DROP COLUMN cleaned_at")
    op.execute("ALTER TABLE source_artifacts DROP COLUMN cleanup_state")
    op.execute("ALTER TABLE source_artifacts DROP COLUMN retention_expires_at")
    op.execute("ALTER TABLE source_artifacts DROP COLUMN retention_policy")
    op.execute("ALTER TABLE source_artifacts DROP COLUMN metadata_json")
