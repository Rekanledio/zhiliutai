import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from qdrant_client import QdrantClient

from app.core.config import PROJECT_ROOT, Settings, get_settings, sqlite_url_for
from app.services.vector_store import COLLECTION_NAME, QdrantLocalStore, VectorRecord


def test_migration_upgrade_downgrade_upgrade(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "migration.db"
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(database_path),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
    )
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config(str(PROJECT_ROOT / "backend" / "alembic.ini"))
    config.set_main_option(
        "script_location", str(PROJECT_ROOT / "backend" / "app" / "db" / "migrations")
    )
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        assert {
            "knowledge_items",
            "content_versions",
            "processing_jobs",
            "job_attempts",
            "chunks",
            "chunk_fts",
            "tags",
            "knowledge_item_tags",
        }.issubset(tables)
        assert "suggested_collections_json" in {
            row[1]
            for row in connection.execute("PRAGMA table_info(content_versions)")
        }
        assert connection.execute("SELECT sql FROM sqlite_master WHERE name='chunk_fts'").fetchone()
    command.downgrade(config, "base")
    command.upgrade(config, "head")


def test_video_lifecycle_migration_defaults_constraints_and_round_trip(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "video-lifecycle.db"
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(database_path),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
    )
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    config = Config(str(PROJECT_ROOT / "backend" / "alembic.ini"))
    config.set_main_option(
        "script_location", str(PROJECT_ROOT / "backend" / "app" / "db" / "migrations")
    )

    command.upgrade(config, "0003_rag_audit")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO knowledge_items "
            "(id, title, source_type, status, content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("item-1", "视频条目", "video", "processing", "a" * 64, "now", "now"),
        )
        connection.execute(
            "INSERT INTO knowledge_items "
            "(id, title, source_type, status, content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("item-2", "另一个视频条目", "video", "processing", "e" * 64, "now", "now"),
        )
        connection.execute(
            "INSERT INTO content_versions "
            "(id, knowledge_item_id, version_no, source_kind, title, body, content_hash, "
            "suggested_tags_json, source_metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "version-1",
                "item-1",
                1,
                "video",
                "视频条目",
                "正文",
                "b" * 64,
                "[]",
                "{}",
                "now",
            ),
        )
        connection.execute(
            "INSERT INTO content_versions "
            "(id, knowledge_item_id, version_no, source_kind, title, body, content_hash, "
            "suggested_tags_json, source_metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "version-b",
                "item-2",
                1,
                "video",
                "另一个视频条目",
                "另一个正文",
                "f" * 64,
                "[]",
                "{}",
                "now",
            ),
        )
        connection.execute(
            "INSERT INTO source_artifacts "
            "(id, knowledge_item_id, artifact_type, media_type, relative_path, "
            "content_hash, byte_size, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "artifact-1",
                "item-1",
                "video_source",
                "video/mp4",
                "video/source.mp4",
                "c" * 64,
                10,
                "now",
            ),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        source_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(source_artifacts)")
        }
        assert {
            "metadata_json",
            "retention_policy",
            "retention_expires_at",
            "cleanup_state",
            "cleaned_at",
        }.issubset(source_columns)
        item_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(knowledge_items)")
        }
        assert "pending_content_version_id" in item_columns
        defaults = connection.execute(
            "SELECT metadata_json, retention_policy, cleanup_state, cleaned_at "
            "FROM source_artifacts WHERE id='artifact-1'"
        ).fetchone()
        assert defaults == ("{}", "permanent", "not_due", None)
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(source_artifacts)")
        }
        assert "ix_source_artifacts_cleanup_due" in indexes
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "trg_knowledge_items_pending_version_owner_insert",
            "trg_knowledge_items_pending_version_owner_update",
            "trg_content_versions_pending_version_owner_update",
        }.issubset(trigger_names)

        connection.execute(
            "INSERT INTO content_versions "
            "(id, knowledge_item_id, version_no, source_kind, title, body, content_hash, "
            "suggested_tags_json, source_metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "version-2",
                "item-1",
                2,
                "video",
                "视频条目重处理",
                "待审核正文",
                "d" * 64,
                "[]",
                "{}",
                "now",
            ),
        )
        connection.execute(
            "UPDATE knowledge_items SET current_content_version_id=?, "
            "pending_content_version_id=? WHERE id=?",
            ("version-1", "version-2", "item-1"),
        )
        assert connection.execute(
            "SELECT current_content_version_id, pending_content_version_id "
            "FROM knowledge_items WHERE id='item-1'"
        ).fetchone() == ("version-1", "version-2")
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE knowledge_items SET pending_content_version_id='version-b' "
                "WHERE id='item-1'"
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_items "
                "(id, title, source_type, status, content_hash, "
                "pending_content_version_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "item-cross-insert",
                    "跨条目插入",
                    "video",
                    "processing",
                    "g" * 64,
                    "version-b",
                    "now",
                    "now",
                ),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE knowledge_items SET pending_content_version_id='missing-version' "
                "WHERE id='item-1'"
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE knowledge_items SET pending_content_version_id=current_content_version_id "
                "WHERE id='item-1'"
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE content_versions SET knowledge_item_id='item-2' "
                "WHERE id='version-2'"
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_artifacts SET cleanup_state='deleted' WHERE id='artifact-1'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_artifacts SET retention_policy='unknown' WHERE id='artifact-1'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_artifacts SET retention_policy='until_expiry' "
                "WHERE id='artifact-1'"
            )
        connection.rollback()
        connection.execute(
            "UPDATE source_artifacts SET retention_policy='until_expiry', "
            "retention_expires_at='tomorrow' WHERE id='artifact-1'"
        )
        assert connection.execute(
            "SELECT retention_policy, retention_expires_at "
            "FROM source_artifacts WHERE id='artifact-1'"
        ).fetchone() == ("until_expiry", "tomorrow")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_artifacts SET cleanup_state='unknown' WHERE id='artifact-1'"
            )
        connection.rollback()
        connection.execute(
            "UPDATE source_artifacts SET cleanup_state='deleted', cleaned_at='now' "
            "WHERE id='artifact-1'"
        )
        connection.execute(
            "DELETE FROM content_versions WHERE id='version-2'"
        )
        assert connection.execute(
            "SELECT current_content_version_id, pending_content_version_id "
            "FROM knowledge_items WHERE id='item-1'"
        ).fetchone() == ("version-1", None)
        connection.commit()

    command.downgrade(config, "0003_rag_audit")
    with sqlite3.connect(database_path) as connection:
        assert "pending_content_version_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(knowledge_items)")
        }
        assert "metadata_json" not in {
            row[1] for row in connection.execute("PRAGMA table_info(source_artifacts)")
        }
        assert not {
            "trg_knowledge_items_pending_version_owner_insert",
            "trg_knowledge_items_pending_version_owner_update",
            "trg_content_versions_pending_version_owner_update",
        }.intersection(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        )
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute(
            "SELECT id, metadata_json, retention_policy, cleanup_state "
            "FROM source_artifacts WHERE id='artifact-1'"
        ).fetchone() == ("artifact-1", "{}", "permanent", "not_due")
        assert connection.execute(
            "SELECT current_content_version_id, pending_content_version_id "
            "FROM knowledge_items WHERE id='item-1'"
        ).fetchone() == ("version-1", None)
        assert connection.execute(
            "SELECT knowledge_item_id FROM content_versions WHERE id='version-b'"
        ).fetchone() == ("item-2",)
        assert {
            "trg_knowledge_items_pending_version_owner_insert",
            "trg_knowledge_items_pending_version_owner_update",
            "trg_content_versions_pending_version_owner_update",
        }.issubset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        )


def test_qdrant_local_persists_payload_and_retrieves(tmp_path: Path) -> None:
    path = tmp_path / "qdrant"
    store = QdrantLocalStore(path, 3)
    record = VectorRecord(
        point_id="6bde267c-7e74-4e20-9470-4d30fc5ea3c1",
        vector=[1.0, 0.0, 0.0],
        chunk_id="chunk-1",
        knowledge_item_id="item-1",
        content_version_id="version-1",
        source_type="markdown",
        source_locator="Notes/test.md",
        embedding_model="fake",
        embedding_version="v1",
    )
    store.upsert([record])
    reopened = QdrantLocalStore(path, 3)
    result = reopened.search([1.0, 0.0, 0.0], limit=1)
    assert result[0]["payload"] == record.payload()
    current = VectorRecord(
        point_id="58145d8e-726d-4c89-98ca-cfbb7aa2a2c3",
        vector=[0.0, 1.0, 0.0],
        chunk_id="chunk-2",
        knowledge_item_id="item-1",
        content_version_id="version-2",
        source_type="markdown",
        source_locator="Notes/test.md",
        embedding_model="fake",
        embedding_version="v1",
    )
    reopened.upsert([current])
    reopened.delete_item_except_version("item-1", "version-2")
    current_results = reopened.search([0.0, 1.0, 0.0], limit=10)
    assert [point["payload"]["content_version_id"] for point in current_results] == ["version-2"]
    client = QdrantClient(path=str(path))
    try:
        assert client.count(COLLECTION_NAME).count == 1
    finally:
        client.close()
    reopened.delete_item("item-1")
    client = QdrantClient(path=str(path))
    try:
        assert client.count(COLLECTION_NAME).count == 0
    finally:
        client.close()
