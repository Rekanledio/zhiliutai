import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from qdrant_client import QdrantClient

from app.core.config import PROJECT_ROOT, Settings, get_settings, sqlite_url_for
from app.services.vector_store import COLLECTION_NAME, QdrantLocalStore, VectorRecord


def test_migration_upgrade_downgrade_upgrade(
    tmp_path: Path, monkeypatch
) -> None:
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
        }.issubset(tables)
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='chunk_fts'"
        ).fetchone()
    command.downgrade(config, "base")
    command.upgrade(config, "head")


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
    client = QdrantClient(path=str(path))
    try:
        assert client.count(COLLECTION_NAME).count == 1
    finally:
        client.close()
