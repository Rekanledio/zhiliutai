from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.services.backup as backup_module
from app.core.config import Settings, sqlite_url_for
from app.db.session import create_engine
from app.main import create_app
from app.rag.retrieval import HybridRetriever
from app.services.backup import (
    BackupError,
    BackupRestoreService,
    RestoreTargets,
    restore_targets_for_settings,
)
from conftest import FakeDraftProvider, FakeEmbeddingProvider, migrate, wait_for_job


def _settings_for_root(root: Path, *, with_vault: bool = True) -> Settings:
    vault = root / "vault"
    if with_vault:
        vault.mkdir(parents=True, exist_ok=True)
    return Settings(
        _env_file=None,
        database_url=sqlite_url_for(root / "zhiliutai.db"),
        qdrant_path=root / "qdrant",
        artifact_root=root / "artifacts",
        workflow_checkpoint_path=root / "checkpoints" / "workflows.db",
        vault_path=str(vault) if with_vault else None,
        embedding_dimensions=8,
        obsidian_watch_interval_seconds=0.05,
        health_check_timeout=0.05,
    )


def _dispose(engine) -> None:
    asyncio.run(engine.dispose())


def _make_minimal_backup(settings: Settings, monkeypatch, destination: Path) -> Path:
    migrate(settings, monkeypatch)
    settings.workflow_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.workflow_checkpoint_path) as connection:
        connection.execute("CREATE TABLE checkpoint_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO checkpoint_marker VALUES ('synthetic')")
    engine = create_engine(settings)
    try:
        service = BackupRestoreService(settings, async_sessionmaker(engine, expire_on_commit=False))
        result = service.create_backup(destination)
        assert result.manifest.schema_revision == "0006_collections"
    finally:
        _dispose(engine)
    return destination


def _rewrite_zip(source: Path, destination: Path, replacement: dict[str, bytes]) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as rewritten:
        for info in original.infolist():
            data = replacement.get(info.filename, original.read(info.filename))
            rewritten.writestr(info, data)


def test_backup_restore_rebuild_round_trip_includes_checkpoint_and_excludes_qdrant(
    settings, monkeypatch, tmp_path: Path
) -> None:
    migrate(settings, monkeypatch)
    app = create_app(
        settings,
        FakeDraftProvider(),
        FakeEmbeddingProvider(),
        start_background=True,
        serve_frontend=False,
    )
    archive = tmp_path / "backup.zip"
    item_id: str
    with TestClient(app) as http:
        submitted = http.post(
            "/api/sources/text",
            json={
                "content": "备份恢复的权威正文\n\n这里是第二段可检索证据。",
                "source_type": "markdown",
            },
        )
        assert submitted.status_code == 202, submitted.text
        submission = submitted.json()
        wait_for_job(http, submission["job_id"])
        item_id = submission["item_id"]
        assert http.post(f"/api/items/{item_id}/review", json={"approved": True}).status_code == 200
        published = http.post(f"/api/items/{item_id}/publish")
        assert published.status_code == 200, published.text

        service = BackupRestoreService(
            settings,
            http.app.state.session_factory,
            FakeEmbeddingProvider(),
        )
        backup_result = service.create_backup(archive)
        assert backup_result.manifest.files
        assert backup_result.manifest.model_dump(mode="json")["rebuild_required"] is True

    with zipfile.ZipFile(archive) as archive_file:
        names = set(archive_file.namelist())
        manifest_bytes = archive_file.read("manifest.json")
    assert "data/checkpoint.sqlite" in names
    assert not any(name.startswith("qdrant/") for name in names)
    assert str(settings.vault_root).encode() not in manifest_bytes

    for path in (
        settings.database_path,
        settings.workflow_checkpoint_path,
        settings.workflow_checkpoint_path.with_name(
            settings.workflow_checkpoint_path.name + "-wal"
        ),
        settings.workflow_checkpoint_path.with_name(
            settings.workflow_checkpoint_path.name + "-shm"
        ),
    ):
        path.unlink(missing_ok=True)
    for directory in (settings.artifact_root, settings.qdrant_path, settings.vault_root):
        if directory is not None and directory.exists():
            shutil.rmtree(directory)

    restored_root = tmp_path / "restored"
    restored_settings = _settings_for_root(restored_root)
    restored_engine = create_engine(restored_settings)
    restored_factory = async_sessionmaker(restored_engine, expire_on_commit=False)
    try:
        restored_service = BackupRestoreService(
            restored_settings,
            restored_factory,
            FakeEmbeddingProvider(),
        )
        restored = restored_service.restore_backup(
            archive,
            restore_targets_for_settings(restored_settings),
            offline=True,
        )
        assert restored.checkpoint_restored is True
        rebuilt = asyncio.run(restored_service.rebuild_derived_state())
        assert rebuilt.published_items == 1
        assert rebuilt.chunks >= 1

        async def inspect_rebuilt() -> tuple[int, list[str]]:
            async with restored_factory() as session:
                fts_count = (
                    await session.execute(text("SELECT COUNT(*) FROM chunk_fts"))
                ).scalar_one()
            retriever = HybridRetriever(
                restored_factory,
                restored_service.vector_store,
                FakeEmbeddingProvider(),
                restored_settings,
            )
            chunks, _diagnostics, _assessment = await retriever.retrieve("权威正文", limit=3)
            return int(fts_count), [chunk.knowledge_item_id for chunk in chunks]

        fts_count, found_item_ids = asyncio.run(inspect_rebuilt())
        assert fts_count == rebuilt.chunks
        assert item_id in found_item_ids
    finally:
        _dispose(restored_engine)


def test_restore_rejects_corruption_traversal_symlink_version_and_existing_target(
    settings, monkeypatch, tmp_path: Path
) -> None:
    archive = _make_minimal_backup(settings, monkeypatch, tmp_path / "valid.zip")
    engine = create_engine(settings)
    service = BackupRestoreService(settings, async_sessionmaker(engine, expire_on_commit=False))
    restore_root = tmp_path / "restore"
    targets = RestoreTargets(
        restore_root / "db.sqlite",
        restore_root / "checkpoint.sqlite",
        restore_root / "artifacts",
        restore_root / "vault",
    )
    try:
        corrupt = tmp_path / "corrupt.zip"
        _rewrite_zip(archive, corrupt, {"data/business.sqlite": b"corrupt"})
        with pytest.raises(BackupError) as hash_error:
            service.restore_backup(corrupt, targets, offline=True)
        assert hash_error.value.code == "archive_hash_mismatch"
        assert not restore_root.exists()

        traversal = tmp_path / "traversal.zip"
        with zipfile.ZipFile(archive) as original, zipfile.ZipFile(traversal, "w") as rewritten:
            for info in original.infolist():
                rewritten.writestr(info, original.read(info.filename))
            rewritten.writestr("../escape.txt", b"ESCAPE_SENTINEL")
        with pytest.raises(BackupError) as traversal_error:
            service.restore_backup(traversal, targets, offline=True)
        assert traversal_error.value.code == "unsafe_archive_path"
        assert not (tmp_path / "escape.txt").exists()

        symlink_archive = tmp_path / "symlink.zip"
        with zipfile.ZipFile(archive) as original, zipfile.ZipFile(
            symlink_archive, "w"
        ) as rewritten:
            for info in original.infolist():
                rewritten.writestr(info, original.read(info.filename))
            symlink = zipfile.ZipInfo("vault/link")
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            rewritten.writestr(symlink, b"TARGET_SENTINEL")
        with pytest.raises(BackupError) as symlink_error:
            service.restore_backup(symlink_archive, targets, offline=True)
        assert symlink_error.value.code == "archive_symlink"

        incompatible = tmp_path / "incompatible.zip"
        with zipfile.ZipFile(archive) as original:
            manifest = json.loads(original.read("manifest.json"))
        manifest["schema_revision"] = "0005_workflow_requests"
        _rewrite_zip(
            archive,
            incompatible,
            {"manifest.json": json.dumps(manifest, separators=(",", ":")).encode()},
        )
        with pytest.raises(BackupError) as version_error:
            service.restore_backup(incompatible, targets, offline=True)
        assert version_error.value.code == "schema_incompatible"

        restore_root.mkdir()
        (restore_root / "db.sqlite").write_bytes(b"existing")
        with pytest.raises(BackupError) as existing_error:
            service.restore_backup(archive, targets, offline=True)
        assert existing_error.value.code == "restore_target_exists"
        assert (restore_root / "db.sqlite").read_bytes() == b"existing"
    finally:
        _dispose(engine)


def test_restore_explicit_overwrite_rolls_back_interruption(
    settings, monkeypatch, tmp_path: Path
) -> None:
    archive = _make_minimal_backup(settings, monkeypatch, tmp_path / "valid.zip")
    engine = create_engine(settings)
    service = BackupRestoreService(settings, async_sessionmaker(engine, expire_on_commit=False))
    restore_root = tmp_path / "restore"
    targets = RestoreTargets(
        restore_root / "db.sqlite",
        restore_root / "checkpoint.sqlite",
        restore_root / "artifacts",
        restore_root / "vault",
    )
    restore_root.mkdir()
    targets.database_path.write_bytes(b"old-db")
    targets.checkpoint_path.write_bytes(b"old-checkpoint")
    targets.artifact_root.mkdir()
    (targets.artifact_root / "old.txt").write_text("old-artifact", encoding="utf-8")
    targets.managed_vault_root.mkdir()
    (targets.managed_vault_root / "old.md").write_text("old-vault", encoding="utf-8")
    original_replace = backup_module.os.replace
    replace_calls = 0

    def fail_on_artifact_install(source: Path, target: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 6:
            raise OSError("INTERRUPTION_SENTINEL")
        original_replace(source, target)

    try:
        monkeypatch.setattr(backup_module.os, "replace", fail_on_artifact_install)
        with pytest.raises(BackupError) as interrupted:
            service.restore_backup(archive, targets, allow_overwrite=True, offline=True)
        assert interrupted.value.code == "restore_interrupted"
        assert targets.database_path.read_bytes() == b"old-db"
        assert targets.checkpoint_path.read_bytes() == b"old-checkpoint"
        assert (targets.artifact_root / "old.txt").read_text(encoding="utf-8") == "old-artifact"
        assert (targets.managed_vault_root / "old.md").read_text(encoding="utf-8") == "old-vault"
        assert not list(restore_root.glob(".*.restore-*"))
        monkeypatch.setattr(backup_module.os, "replace", original_replace)
        restored = service.restore_backup(archive, targets, allow_overwrite=True, offline=True)
        assert restored.files_restored >= 2
        with sqlite3.connect(targets.database_path) as connection:
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == ("0006_collections",)
    finally:
        _dispose(engine)


def test_restore_requires_offline_and_replaces_all_sqlite_sidecars(
    settings, monkeypatch, tmp_path: Path
) -> None:
    archive = _make_minimal_backup(settings, monkeypatch, tmp_path / "valid.zip")
    engine = create_engine(settings)
    service = BackupRestoreService(settings, async_sessionmaker(engine, expire_on_commit=False))
    restore_root = tmp_path / "restore-sidecars"
    targets = RestoreTargets(
        restore_root / "db.sqlite",
        restore_root / "checkpoint.sqlite",
        restore_root / "artifacts",
        restore_root / "vault",
    )
    try:
        with pytest.raises(BackupError) as offline_error:
            service.restore_backup(archive, targets)
        assert offline_error.value.code == "restore_requires_offline"

        restore_root.mkdir()
        targets.database_path.write_bytes(b"OLD_DB_SENTINEL")
        targets.checkpoint_path.write_bytes(b"OLD_CHECKPOINT_SENTINEL")
        for path, value in (
            (targets.database_path.with_name("db.sqlite-wal"), b"OLD_DB_WAL_SENTINEL"),
            (targets.database_path.with_name("db.sqlite-shm"), b"OLD_DB_SHM_SENTINEL"),
            (
                targets.checkpoint_path.with_name("checkpoint.sqlite-wal"),
                b"OLD_CHECKPOINT_WAL_SENTINEL",
            ),
            (
                targets.checkpoint_path.with_name("checkpoint.sqlite-shm"),
                b"OLD_CHECKPOINT_SHM_SENTINEL",
            ),
        ):
            path.write_bytes(value)
        targets.artifact_root.mkdir()
        targets.managed_vault_root.mkdir()

        result = service.restore_backup(
            archive,
            targets,
            allow_overwrite=True,
            offline=True,
        )
        assert result.checkpoint_restored is True
        sidecars = (
            targets.database_path.with_name("db.sqlite-wal"),
            targets.database_path.with_name("db.sqlite-shm"),
            targets.checkpoint_path.with_name("checkpoint.sqlite-wal"),
            targets.checkpoint_path.with_name("checkpoint.sqlite-shm"),
        )
        assert all(not path.exists() for path in sidecars)
        with sqlite3.connect(targets.database_path) as connection:
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == ("0006_collections",)
        with sqlite3.connect(targets.checkpoint_path) as connection:
            assert connection.execute(
                "SELECT value FROM checkpoint_marker"
            ).fetchone() == ("synthetic",)
        for path in sidecars:
            if path.exists():
                assert b"OLD_" not in path.read_bytes()
        assert not list(restore_root.glob(".*.restore-*"))
    finally:
        _dispose(engine)


def test_restore_rolls_back_old_main_and_sidecars_on_partial_install_and_cleanup_failure(
    settings, monkeypatch, tmp_path: Path
) -> None:
    archive = _make_minimal_backup(settings, monkeypatch, tmp_path / "valid.zip")
    engine = create_engine(settings)
    service = BackupRestoreService(settings, async_sessionmaker(engine, expire_on_commit=False))
    restore_root = tmp_path / "restore-rollback"
    targets = RestoreTargets(
        restore_root / "db.sqlite",
        restore_root / "checkpoint.sqlite",
        restore_root / "artifacts",
        restore_root / "vault",
    )
    restore_root.mkdir()
    old_files = {
        targets.database_path: b"OLD_DB_SENTINEL",
        targets.database_path.with_name("db.sqlite-wal"): b"OLD_DB_WAL_SENTINEL",
        targets.database_path.with_name("db.sqlite-shm"): b"OLD_DB_SHM_SENTINEL",
        targets.checkpoint_path: b"OLD_CHECKPOINT_SENTINEL",
        targets.checkpoint_path.with_name("checkpoint.sqlite-wal"): b"OLD_CHECKPOINT_WAL_SENTINEL",
        targets.checkpoint_path.with_name("checkpoint.sqlite-shm"): b"OLD_CHECKPOINT_SHM_SENTINEL",
    }
    for path, value in old_files.items():
        path.write_bytes(value)
    targets.artifact_root.mkdir()
    (targets.artifact_root / "old.txt").write_text("old-artifact", encoding="utf-8")
    targets.managed_vault_root.mkdir()
    (targets.managed_vault_root / "old.md").write_text("old-vault", encoding="utf-8")
    original_replace = backup_module.os.replace
    replace_calls = 0

    def fail_after_sidecars(source: Path, target: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 9:
            raise OSError("PARTIAL_INSTALL_SENTINEL")
        original_replace(source, target)

    try:
        monkeypatch.setattr(backup_module.os, "replace", fail_after_sidecars)
        with pytest.raises(BackupError) as interrupted:
            service.restore_backup(archive, targets, allow_overwrite=True, offline=True)
        assert interrupted.value.code == "restore_interrupted"
        for path, value in old_files.items():
            assert path.read_bytes() == value
        assert (targets.artifact_root / "old.txt").read_text(encoding="utf-8") == "old-artifact"
        assert (targets.managed_vault_root / "old.md").read_text(encoding="utf-8") == "old-vault"
        assert not list(restore_root.glob(".*.restore-*"))

        monkeypatch.setattr(backup_module.os, "replace", original_replace)
        original_remove = service._remove_exact
        failed_once = False

        def fail_cleanup(path: Path) -> None:
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise OSError("CLEANUP_SENTINEL")
            original_remove(path)

        monkeypatch.setattr(service, "_remove_exact", fail_cleanup)
        with pytest.raises(BackupError) as cleanup_error:
            service.restore_backup(archive, targets, allow_overwrite=True, offline=True)
        assert cleanup_error.value.code == "restore_cleanup_failed"
        for path, value in old_files.items():
            assert path.read_bytes() == value
        assert (targets.artifact_root / "old.txt").read_text(encoding="utf-8") == "old-artifact"
        assert (targets.managed_vault_root / "old.md").read_text(encoding="utf-8") == "old-vault"
        assert not list(restore_root.glob(".*.restore-*"))
    finally:
        _dispose(engine)


def test_restore_rejects_database_checkpoint_sidecar_and_root_overlap(
    settings, monkeypatch, tmp_path: Path
) -> None:
    archive = _make_minimal_backup(settings, monkeypatch, tmp_path / "valid.zip")
    engine = create_engine(settings)
    service = BackupRestoreService(settings, async_sessionmaker(engine, expire_on_commit=False))
    try:
        overlapping = RestoreTargets(
            tmp_path / "db.sqlite",
            tmp_path / "db.sqlite-wal",
            tmp_path / "artifacts",
            tmp_path / "vault",
        )
        with pytest.raises(BackupError) as sidecar_error:
            service.restore_backup(archive, overlapping, offline=True)
        assert sidecar_error.value.code == "invalid_target"

        root_overlap = RestoreTargets(
            tmp_path / "database.sqlite",
            tmp_path / "checkpoint.sqlite",
            tmp_path / "database.sqlite-wal",
            tmp_path / "vault-root",
        )
        with pytest.raises(BackupError) as root_error:
            service.restore_backup(archive, root_overlap, offline=True)
        assert root_error.value.code == "invalid_target"
    finally:
        _dispose(engine)


def test_restore_rejects_archive_target_and_staging_parent_collisions(
    settings, monkeypatch, tmp_path: Path
) -> None:
    archive = _make_minimal_backup(settings, monkeypatch, tmp_path / "valid.zip")
    engine = create_engine(settings)
    service = BackupRestoreService(settings, async_sessionmaker(engine, expire_on_commit=False))
    try:
        base_targets = RestoreTargets(
            tmp_path / "restore" / "database.sqlite",
            tmp_path / "restore" / "checkpoint.sqlite",
            tmp_path / "restore" / "artifacts",
            tmp_path / "restore" / "vault",
        )
        archive_as_database = RestoreTargets(
            archive,
            base_targets.checkpoint_path,
            base_targets.artifact_root,
            base_targets.managed_vault_root,
        )
        with pytest.raises(BackupError) as database_error:
            service.restore_backup(archive, archive_as_database, offline=True)
        assert database_error.value.code == "invalid_target"

        checkpoint_sidecar = tmp_path / "checkpoint.sqlite-wal"
        shutil.copy2(archive, checkpoint_sidecar)
        sidecar_archive = checkpoint_sidecar
        archive_as_checkpoint_sidecar = RestoreTargets(
            base_targets.database_path,
            tmp_path / "checkpoint.sqlite",
            base_targets.artifact_root,
            base_targets.managed_vault_root,
        )
        with pytest.raises(BackupError) as sidecar_error:
            service.restore_backup(
                sidecar_archive,
                archive_as_checkpoint_sidecar,
                offline=True,
            )
        assert sidecar_error.value.code == "invalid_target"

        artifact_root = tmp_path / "archive-in-artifacts"
        artifact_root.mkdir()
        artifact_archive = artifact_root / "backup.zip"
        shutil.copy2(archive, artifact_archive)
        archive_in_artifacts = RestoreTargets(
            tmp_path / "restore-artifacts" / "database.sqlite",
            tmp_path / "restore-artifacts" / "checkpoint.sqlite",
            artifact_root,
            tmp_path / "restore-artifacts" / "vault",
        )
        with pytest.raises(BackupError) as artifact_error:
            service.restore_backup(artifact_archive, archive_in_artifacts, offline=True)
        assert artifact_error.value.code == "invalid_target"

        vault_root = tmp_path / "archive-in-vault"
        vault_root.mkdir()
        vault_archive = vault_root / "backup.zip"
        shutil.copy2(archive, vault_archive)
        archive_in_vault = RestoreTargets(
            tmp_path / "restore-vault" / "database.sqlite",
            tmp_path / "restore-vault" / "checkpoint.sqlite",
            tmp_path / "restore-vault" / "artifacts",
            vault_root,
        )
        with pytest.raises(BackupError) as vault_error:
            service.restore_backup(vault_archive, archive_in_vault, offline=True)
        assert vault_error.value.code == "invalid_target"

        forced_staging = tmp_path / "forced-staging"

        class FixedTemporaryDirectory:
            def __init__(self, *_args, **_kwargs) -> None:
                forced_staging.mkdir()
                self.name = str(forced_staging)

            def cleanup(self) -> None:
                shutil.rmtree(forced_staging, ignore_errors=True)

        staging_collision = RestoreTargets(
            tmp_path / "staging-collision" / "database.sqlite",
            tmp_path / "staging-collision" / "checkpoint.sqlite",
            forced_staging,
            tmp_path / "staging-collision" / "vault",
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                backup_module.tempfile,
                "TemporaryDirectory",
                FixedTemporaryDirectory,
            )
            with pytest.raises(BackupError) as staging_error:
                service.restore_backup(archive, staging_collision, offline=True)
        assert staging_error.value.code == "invalid_target"

        independent = RestoreTargets(
            tmp_path / "independent-ok" / "database.sqlite",
            tmp_path / "independent-ok" / "checkpoint.sqlite",
            tmp_path / "independent-ok" / "artifacts",
            tmp_path / "independent-ok" / "vault",
        )
        result = service.restore_backup(archive, independent, offline=True)
        assert result.files_restored >= 2
        assert independent.database_path.is_file()
        assert independent.checkpoint_path.is_file()
    finally:
        _dispose(engine)


def test_empty_environment_starts_degraded_without_secrets(settings, monkeypatch) -> None:
    empty = Settings(
        _env_file=None,
        database_url=settings.database_url,
        qdrant_path=settings.qdrant_path,
        artifact_root=settings.artifact_root,
        workflow_checkpoint_path=settings.workflow_checkpoint_path,
        vault_path=None,
        chat_api_key="API_KEY_SENTINEL",
    )
    migrate(empty, monkeypatch)
    app = create_app(empty, start_background=False, serve_frontend=False)
    with TestClient(app) as http:
        response = http.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        states = {component["key"]: component["state"] for component in body["components"]}
        assert states["obsidian"] == "not_configured"
        assert states["model_providers"] == "not_configured"
        assert "API_KEY_SENTINEL" not in response.text
    assert empty.api_host == "127.0.0.1"
