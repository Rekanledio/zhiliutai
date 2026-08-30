"""Safe backup, restore, and derived-index rebuild primitives.

The archive contains a versioned manifest, consistent SQLite snapshots, and
only fixed-prefix relative Artifact/Vault files. Qdrant is rebuildable derived
state and is deliberately never copied into the archive.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, Field, StrictInt, StrictStr, ValidationError, field_validator
from pydantic.main import BaseModel
from sqlalchemy import delete, select, text

from app.core.config import Settings
from app.core.paths import safe_relative_path
from app.db.models import Chunk, ContentVersion, KnowledgeItem, NoteBinding, SourceArtifact
from app.obsidian.markdown import ObsidianVault
from app.services.artifacts import ArtifactStore
from app.services.content import content_hash
from app.services.indexing import IndexService
from app.services.vector_store import QdrantLocalStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.providers.models import EmbeddingProvider


BACKUP_FORMAT_VERSION = 1
HEAD_SCHEMA_REVISION = "0007_tags_and_review_suggestions"
MANIFEST_NAME = "manifest.json"
DATABASE_MEMBER = "data/business.sqlite"
CHECKPOINT_MEMBER = "data/checkpoint.sqlite"
ARTIFACT_PREFIX = "artifacts/"
VAULT_PREFIX = "vault/"
MAX_ARCHIVE_ENTRIES = 100_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1_000_000
MAX_COPY_BUFFER = 1024 * 1024

_BACKUP_ERROR_CODES = frozenset(
    {
        "invalid_target",
        "backup_target_exists",
        "database_missing",
        "vault_not_configured",
        "schema_incompatible",
        "backup_failed",
        "archive_invalid",
        "archive_hash_mismatch",
        "archive_too_large",
        "unsafe_archive_path",
        "archive_symlink",
        "restore_target_exists",
        "restore_requires_offline",
        "restore_interrupted",
        "restore_cleanup_failed",
        "rebuild_failed",
        "embedding_not_configured",
        "vault_state_invalid",
        "artifact_state_invalid",
    }
)


class BackupError(Exception):
    """Stable backup boundary error without filesystem or provider details."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _BACKUP_ERROR_CODES else "backup_failed"
        super().__init__(f"backup_{self.code}")


class BackupFileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: StrictStr = Field(min_length=1, max_length=500)
    role: Literal["database", "checkpoint", "artifact", "vault"]
    byte_size: StrictInt = Field(ge=0, le=MAX_MEMBER_BYTES)
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_relative_member(cls, value: str) -> str:
        if safe_relative_path(value) is None:
            raise ValueError("archive member must be relative")
        return value


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format_version: Literal[1] = BACKUP_FORMAT_VERSION
    schema_revision: StrictStr = HEAD_SCHEMA_REVISION
    checkpoint_schema: Literal["opaque_langgraph_sqlite"] = "opaque_langgraph_sqlite"
    derived_stores: list[Literal["qdrant"]] = ["qdrant"]
    rebuild_required: Literal[True] = True
    created_at: StrictStr
    files: list[BackupFileRecord] = Field(max_length=MAX_ARCHIVE_ENTRIES)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: str) -> str:
        if len(value) > 80 or any(ord(character) < 0x20 for character in value):
            raise ValueError("invalid timestamp")
        return value

    @field_validator("derived_stores")
    @classmethod
    def validate_derived_stores(cls, values: list[str]) -> list[str]:
        if values != ["qdrant"]:
            raise ValueError("unsupported derived store manifest")
        return values

    @field_validator("files")
    @classmethod
    def validate_file_roles(cls, values: list[BackupFileRecord]) -> list[BackupFileRecord]:
        paths = [record.path for record in values]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate archive member")
        database_records = [record for record in values if record.role == "database"]
        if [record.path for record in database_records] != [DATABASE_MEMBER]:
            raise ValueError("database snapshot is required")
        for record in values:
            if record.role == "checkpoint" and record.path != CHECKPOINT_MEMBER:
                raise ValueError("invalid checkpoint member")
            if record.role == "artifact" and not record.path.startswith(ARTIFACT_PREFIX):
                raise ValueError("invalid artifact member")
            if record.role == "vault" and not record.path.startswith(VAULT_PREFIX):
                raise ValueError("invalid vault member")
        return values


class RestoreTargets:
    """Explicit absolute restore destinations; none are inferred from an archive."""

    def __init__(
        self,
        database_path: Path,
        checkpoint_path: Path,
        artifact_root: Path,
        managed_vault_root: Path,
    ) -> None:
        self.database_path = database_path
        self.checkpoint_path = checkpoint_path
        self.artifact_root = artifact_root
        self.managed_vault_root = managed_vault_root


class BackupResult:
    def __init__(self, manifest: BackupManifest, archive_sha256: str) -> None:
        self.manifest = manifest
        self.archive_sha256 = archive_sha256


class RestoreResult:
    def __init__(self, manifest: BackupManifest, files_restored: int, checkpoint_restored: bool) -> None:
        self.manifest = manifest
        self.files_restored = files_restored
        self.checkpoint_restored = checkpoint_restored


class RebuildResult:
    def __init__(self, published_items: int, chunks: int) -> None:
        self.published_items = published_items
        self.chunks = chunks


def restore_targets_for_settings(settings: Settings) -> RestoreTargets:
    managed_vault_root = settings.managed_vault_root
    if managed_vault_root is None:
        raise BackupError("vault_not_configured")
    return RestoreTargets(
        settings.database_path,
        settings.workflow_checkpoint_path,
        settings.artifact_root,
        managed_vault_root,
    )


def _resolved_absolute(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise BackupError("invalid_target")
    raw = path.absolute()
    if any(part == ".." for part in raw.parts):
        raise BackupError("invalid_target")
    _reject_symlink_components(raw.parent if raw.suffix else raw)
    if raw.is_symlink():
        raise BackupError("invalid_target")
    return raw.resolve(strict=False)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise BackupError("invalid_target")


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _target_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")


def _paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _copy_exact(source: Path, destination: Path) -> None:
    if source.is_symlink() or not _path_exists(source):
        raise BackupError("invalid_target")
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def _validate_restore_targets(targets: RestoreTargets) -> RestoreTargets:
    resolved = RestoreTargets(
        _resolved_absolute(targets.database_path),
        _resolved_absolute(targets.checkpoint_path),
        _resolved_absolute(targets.artifact_root),
        _resolved_absolute(targets.managed_vault_root),
    )
    all_paths = [
        resolved.database_path,
        resolved.checkpoint_path,
        resolved.artifact_root,
        resolved.managed_vault_root,
    ]
    for path in all_paths:
        _reject_symlink_components(path.parent if path.suffix else path)
        if path.is_symlink():
            raise BackupError("invalid_target")
    if resolved.database_path == resolved.checkpoint_path:
        raise BackupError("invalid_target")
    sqlite_paths = list(_target_sidecars(resolved.database_path)) + list(
        _target_sidecars(resolved.checkpoint_path)
    )
    if len(set(sqlite_paths)) != len(sqlite_paths):
        raise BackupError("invalid_target")
    for file_path in sqlite_paths:
        for root in (resolved.artifact_root, resolved.managed_vault_root):
            if (
                file_path == root
                or file_path.is_relative_to(root)
                or root.is_relative_to(file_path)
            ):
                raise BackupError("invalid_target")
    if (
        resolved.artifact_root == resolved.managed_vault_root
        or resolved.artifact_root.is_relative_to(resolved.managed_vault_root)
        or resolved.managed_vault_root.is_relative_to(resolved.artifact_root)
    ):
        raise BackupError("invalid_target")
    return resolved


def _validate_archive_path(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise BackupError("archive_invalid")
    raw = path.absolute()
    if any(part == ".." for part in raw.parts):
        raise BackupError("unsafe_archive_path")
    _reject_symlink_components(raw.parent)
    if raw.is_symlink():
        raise BackupError("archive_symlink")
    resolved = raw.resolve(strict=False)
    if not resolved.is_file():
        raise BackupError("archive_invalid")
    return resolved


def _validate_archive_target_boundary(archive_path: Path, targets: RestoreTargets) -> None:
    sqlite_targets = list(_target_sidecars(targets.database_path)) + list(
        _target_sidecars(targets.checkpoint_path)
    )
    if any(_paths_overlap(archive_path, target) for target in sqlite_targets):
        raise BackupError("invalid_target")
    if any(
        _paths_overlap(archive_path, root)
        for root in (targets.artifact_root, targets.managed_vault_root)
    ):
        raise BackupError("invalid_target")


def _validate_staging_target_boundary(
    staging: Path, archive_path: Path, targets: RestoreTargets
) -> None:
    restore_paths = [
        archive_path,
        *list(_target_sidecars(targets.database_path)),
        *list(_target_sidecars(targets.checkpoint_path)),
        targets.artifact_root,
        targets.managed_vault_root,
    ]
    if any(_paths_overlap(staging, target) for target in restore_paths):
        raise BackupError("invalid_target")


def _hash_file(source: Path, destination: Path | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with source.open("rb") as source_handle:
            target_handle = destination.open("xb") if destination is not None else None
            try:
                while chunk := source_handle.read(MAX_COPY_BUFFER):
                    byte_size += len(chunk)
                    if byte_size > MAX_MEMBER_BYTES:
                        raise BackupError("archive_too_large")
                    digest.update(chunk)
                    if target_handle is not None:
                        target_handle.write(chunk)
            finally:
                if target_handle is not None:
                    target_handle.close()
    except BackupError:
        raise
    except (OSError, ValueError) as error:
        raise BackupError("backup_failed") from error
    return digest.hexdigest(), byte_size


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise BackupError("database_missing")
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(str(source), timeout=10)
        destination_connection = sqlite3.connect(str(destination), timeout=10)
        source_connection.execute("PRAGMA busy_timeout=10000")
        source_connection.backup(destination_connection)
        destination_connection.commit()
    except (OSError, sqlite3.Error) as error:
        raise BackupError("backup_failed") from error
    finally:
        if source_connection is not None:
            source_connection.close()
        if destination_connection is not None:
            destination_connection.close()


def _schema_revision(database_path: Path) -> str:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(database_path), timeout=10)
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.Error as error:
        raise BackupError("schema_incompatible") from error
    finally:
        if connection is not None:
            connection.close()
    if len(rows) != 1 or rows[0][0] != HEAD_SCHEMA_REVISION:
        raise BackupError("schema_incompatible")
    return rows[0][0]


def _validate_sqlite_snapshot(database_path: Path, *, require_alembic: bool) -> None:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(database_path), timeout=10)
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise BackupError("archive_invalid")
        if require_alembic:
            _schema_revision(database_path)
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise BackupError("archive_invalid") from error
    finally:
        if connection is not None:
            connection.close()


def _copy_managed_tree(
    root: Path,
    staging_root: Path,
    prefix: str,
    role: Literal["artifact", "vault"],
    total: list[int],
) -> list[BackupFileRecord]:
    if root.is_symlink():
        raise BackupError("backup_failed")
    if not root.exists():
        return []
    if not root.is_dir():
        raise BackupError("backup_failed")
    try:
        paths = sorted(root.rglob("*"), key=lambda path: path.as_posix())
    except OSError as error:
        raise BackupError("backup_failed") from error
    records: list[BackupFileRecord] = []
    for source in paths:
        if source.is_symlink():
            raise BackupError("backup_failed")
        if source.is_dir():
            continue
        if not source.is_file():
            raise BackupError("backup_failed")
        relative = source.relative_to(root).as_posix()
        safe_relative = safe_relative_path(relative)
        if safe_relative is None:
            raise BackupError("backup_failed")
        if any(part.startswith(".") for part in Path(safe_relative).parts):
            continue
        member = prefix + safe_relative
        target = staging_root / member
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, byte_size = _hash_file(source, target)
        total[0] += byte_size
        if total[0] > MAX_ARCHIVE_BYTES:
            raise BackupError("archive_too_large")
        records.append(BackupFileRecord(path=member, role=role, byte_size=byte_size, sha256=digest))
    return records


class BackupRestoreService:
    """Application service for explicit backup, restore, and rebuild calls."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.embedding_provider = embedding_provider
        self.artifacts = ArtifactStore(settings.artifact_root)
        self.vector_store = QdrantLocalStore(settings.qdrant_path, settings.embedding_dimensions)

    def create_backup(self, destination: Path, *, allow_overwrite: bool = False) -> BackupResult:
        destination = _resolved_absolute(destination)
        if _path_exists(destination) and not allow_overwrite:
            raise BackupError("backup_target_exists")
        managed_vault_root = self.settings.managed_vault_root
        if managed_vault_root is None:
            raise BackupError("vault_not_configured")
        source_roots = [
            self.settings.database_path.resolve(strict=False),
            self.settings.workflow_checkpoint_path.resolve(strict=False),
            self.settings.artifact_root.resolve(strict=False),
            managed_vault_root.resolve(strict=False),
        ]
        if any(destination == root or destination.is_relative_to(root) for root in source_roots):
            raise BackupError("invalid_target")
        if not self.settings.database_path.is_file() or self.settings.database_path.is_symlink():
            raise BackupError("database_missing")
        _reject_symlink_components(destination.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_archive: Path | None = None
        try:
            with tempfile.TemporaryDirectory(
                prefix=".zhiliutai-backup-", dir=str(destination.parent)
            ) as temporary_directory:
                staging = Path(temporary_directory)
                data_dir = staging / "data"
                data_dir.mkdir()
                database_snapshot = data_dir / "business.sqlite"
                _sqlite_snapshot(self.settings.database_path, database_snapshot)
                if _schema_revision(database_snapshot) != HEAD_SCHEMA_REVISION:
                    raise BackupError("schema_incompatible")

                records = [
                    BackupFileRecord(
                        path=DATABASE_MEMBER,
                        role="database",
                        byte_size=database_snapshot.stat().st_size,
                        sha256=_hash_file(database_snapshot)[0],
                    )
                ]
                if _path_exists(self.settings.workflow_checkpoint_path):
                    if (
                        not self.settings.workflow_checkpoint_path.is_file()
                        or self.settings.workflow_checkpoint_path.is_symlink()
                    ):
                        raise BackupError("backup_failed")
                    checkpoint_snapshot = data_dir / "checkpoint.sqlite"
                    _sqlite_snapshot(self.settings.workflow_checkpoint_path, checkpoint_snapshot)
                    _validate_sqlite_snapshot(checkpoint_snapshot, require_alembic=False)
                    records.append(
                        BackupFileRecord(
                            path=CHECKPOINT_MEMBER,
                            role="checkpoint",
                            byte_size=checkpoint_snapshot.stat().st_size,
                            sha256=_hash_file(checkpoint_snapshot)[0],
                        )
                    )

                total = [sum(record.byte_size for record in records)]
                records.extend(
                    _copy_managed_tree(
                        self.settings.artifact_root,
                        staging,
                        ARTIFACT_PREFIX,
                        "artifact",
                        total,
                    )
                )
                records.extend(
                    _copy_managed_tree(
                        managed_vault_root,
                        staging,
                        VAULT_PREFIX,
                        "vault",
                        total,
                    )
                )
                manifest = BackupManifest(
                    created_at=datetime.now(timezone.utc).isoformat(),
                    files=records,
                )
                manifest_bytes = json.dumps(
                    manifest.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    dir=destination.parent,
                    delete=False,
                ) as archive_handle:
                    temporary_archive = Path(archive_handle.name)
                with zipfile.ZipFile(
                    temporary_archive,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    archive.writestr(MANIFEST_NAME, manifest_bytes)
                    for record in records:
                        archive.write(staging / record.path, arcname=record.path)
                with temporary_archive.open("ab") as archive_handle:
                    archive_handle.flush()
                    os.fsync(archive_handle.fileno())
                if _path_exists(destination) and not allow_overwrite:
                    raise BackupError("backup_target_exists")
                os.replace(temporary_archive, destination)
                temporary_archive = None
            archive_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            return BackupResult(manifest, archive_hash)
        except BackupError:
            raise
        except (OSError, RuntimeError, sqlite3.Error, ValidationError, ValueError) as error:
            raise BackupError("backup_failed") from error
        finally:
            if temporary_archive is not None:
                temporary_archive.unlink(missing_ok=True)

    def restore_backup(
        self,
        archive_path: Path,
        targets: RestoreTargets,
        *,
        allow_overwrite: bool = False,
        offline: bool = False,
    ) -> RestoreResult:
        if not offline:
            raise BackupError("restore_requires_offline")
        targets = _validate_restore_targets(targets)
        archive_path = _validate_archive_path(archive_path)
        _validate_archive_target_boundary(archive_path, targets)
        temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        try:
            temporary_directory = tempfile.TemporaryDirectory(
                prefix=".zhiliutai-restore-", dir=str(archive_path.parent)
            )
            staging = Path(temporary_directory.name)
            _validate_staging_target_boundary(staging, archive_path, targets)
            manifest = self._extract_and_validate_archive(archive_path, staging)
            self._validate_restore_preflight(targets, manifest, allow_overwrite=allow_overwrite)
            self._commit_restore(staging, targets, manifest, allow_overwrite=allow_overwrite)
            return RestoreResult(
                manifest,
                len(manifest.files),
                any(record.role == "checkpoint" for record in manifest.files),
            )
        finally:
            if temporary_directory is not None:
                temporary_directory.cleanup()

    def _extract_and_validate_archive(self, archive_path: Path, staging: Path) -> BackupManifest:
        try:
            with zipfile.ZipFile(archive_path, mode="r") as archive:
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_ENTRIES:
                    raise BackupError("archive_too_large")
                info_by_name: dict[str, zipfile.ZipInfo] = {}
                total_size = 0
                for info in infos:
                    name = info.filename
                    if (
                        name in info_by_name
                        or safe_relative_path(name) is None
                        or name.endswith("/")
                        or info.is_dir()
                    ):
                        raise BackupError("unsafe_archive_path")
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        raise BackupError("archive_symlink")
                    if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
                        raise BackupError("archive_too_large")
                    total_size += info.file_size
                    if total_size > MAX_ARCHIVE_BYTES:
                        raise BackupError("archive_too_large")
                    info_by_name[name] = info
                manifest_info = info_by_name.get(MANIFEST_NAME)
                if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
                    raise BackupError("archive_invalid")
                try:
                    manifest_value = json.loads(archive.read(manifest_info))
                    manifest = BackupManifest.model_validate(manifest_value)
                except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as error:
                    raise BackupError("archive_invalid") from error
                if manifest.schema_revision != HEAD_SCHEMA_REVISION:
                    raise BackupError("schema_incompatible")
                expected_names = {record.path for record in manifest.files} | {MANIFEST_NAME}
                if set(info_by_name) != expected_names:
                    raise BackupError("archive_invalid")
                for record in manifest.files:
                    info = info_by_name[record.path]
                    if info.file_size != record.byte_size:
                        raise BackupError("archive_hash_mismatch")
                    target = staging / record.path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    byte_size = 0
                    with archive.open(info, mode="r") as source, target.open("xb") as output:
                        while chunk := source.read(MAX_COPY_BUFFER):
                            byte_size += len(chunk)
                            if byte_size > record.byte_size:
                                raise BackupError("archive_too_large")
                            digest.update(chunk)
                            output.write(chunk)
                    if byte_size != record.byte_size or digest.hexdigest() != record.sha256:
                        raise BackupError("archive_hash_mismatch")
                _validate_sqlite_snapshot(staging / DATABASE_MEMBER, require_alembic=True)
                checkpoint = staging / CHECKPOINT_MEMBER
                if checkpoint.exists():
                    _validate_sqlite_snapshot(checkpoint, require_alembic=False)
                return manifest
        except BackupError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, sqlite3.Error) as error:
            raise BackupError("archive_invalid") from error

    def _validate_restore_preflight(
        self,
        targets: RestoreTargets,
        manifest: BackupManifest,
        *,
        allow_overwrite: bool,
    ) -> None:
        exact_targets = list(_target_sidecars(targets.database_path)) + list(
            _target_sidecars(targets.checkpoint_path)
        )
        exact_targets.extend([targets.artifact_root, targets.managed_vault_root])
        if any(_path_exists(path) for path in exact_targets) and not allow_overwrite:
            raise BackupError("restore_target_exists")
        for path in _target_sidecars(targets.database_path) + _target_sidecars(
            targets.checkpoint_path
        ):
            if _path_exists(path) and (path.is_symlink() or not path.is_file()):
                raise BackupError("invalid_target")
        for directory in (targets.artifact_root, targets.managed_vault_root):
            if _path_exists(directory) and not directory.is_dir():
                raise BackupError("invalid_target")

    @staticmethod
    def _remove_exact(path: Path) -> None:
        if path.is_symlink():
            raise BackupError("invalid_target")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def _commit_restore(
        self,
        staging: Path,
        targets: RestoreTargets,
        manifest: BackupManifest,
        *,
        allow_overwrite: bool,
    ) -> None:
        has_checkpoint = any(record.role == "checkpoint" for record in manifest.files)
        operations: list[tuple[Path, Path | None]] = [
            (targets.database_path, staging / DATABASE_MEMBER),
            (targets.database_path.with_name(targets.database_path.name + "-wal"), None),
            (targets.database_path.with_name(targets.database_path.name + "-shm"), None),
            (
                targets.checkpoint_path,
                staging / CHECKPOINT_MEMBER if has_checkpoint else None,
            ),
            (targets.checkpoint_path.with_name(targets.checkpoint_path.name + "-wal"), None),
            (targets.checkpoint_path.with_name(targets.checkpoint_path.name + "-shm"), None),
            (targets.artifact_root, staging / "artifacts"),
            (targets.managed_vault_root, staging / "vault"),
        ]
        for _target, staged in operations:
            if staged is not None:
                if _target in (targets.database_path, targets.checkpoint_path):
                    staged.parent.mkdir(parents=True, exist_ok=True)
                else:
                    staged.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        moved_old: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        try:
            for target, staged in operations:
                target.parent.mkdir(parents=True, exist_ok=True)
                if _path_exists(target):
                    if not allow_overwrite:
                        raise BackupError("restore_target_exists")
                    backup = target.parent / f".{target.name}.restore-{token}"
                    if _path_exists(backup):
                        raise BackupError("restore_interrupted")
                    os.replace(target, backup)
                    moved_old.append((target, backup))
                if staged is not None:
                    os.replace(staged, target)
                    installed.append(target)
        except BackupError:
            self._rollback_restore(moved_old, installed)
            raise
        except BaseException as error:
            self._rollback_restore(moved_old, installed)
            raise BackupError("restore_interrupted") from error
        cleanup_dirs: dict[Path, Path] = {}
        quarantined: dict[Path, Path] = {}
        rollback_dirs: dict[Path, Path] = {}
        rollback_sources: dict[Path, Path] = {}
        try:
            for target, backup in moved_old:
                cleanup_dir = cleanup_dirs.get(backup.parent)
                if cleanup_dir is None:
                    cleanup_dir = backup.parent / f".zhiliutai-restore-cleanup-{token}"
                    if _path_exists(cleanup_dir):
                        raise BackupError("restore_interrupted")
                    cleanup_dir.mkdir(parents=True, exist_ok=False)
                    cleanup_dirs[backup.parent] = cleanup_dir
                quarantine_path = cleanup_dir / backup.name
                os.replace(backup, quarantine_path)
                quarantined[target] = quarantine_path
            for target, quarantine_path in quarantined.items():
                target_parent = quarantine_path.parent.parent
                rollback_dir = rollback_dirs.get(target_parent)
                if rollback_dir is None:
                    rollback_dir = target_parent / f".zhiliutai-restore-rollback-{token}"
                    if _path_exists(rollback_dir):
                        raise BackupError("restore_interrupted")
                    rollback_dir.mkdir(parents=True, exist_ok=False)
                    rollback_dirs[target_parent] = rollback_dir
                rollback_path = rollback_dir / quarantine_path.name
                _copy_exact(quarantine_path, rollback_path)
                rollback_sources[target] = rollback_path
            for cleanup_dir in cleanup_dirs.values():
                self._remove_exact(cleanup_dir)
            for rollback_dir in rollback_dirs.values():
                try:
                    self._remove_exact(rollback_dir)
                except (OSError, BackupError):
                    pass
        except (OSError, BackupError) as error:
            self._rollback_restore(moved_old, installed, quarantined, rollback_sources)
            for cleanup_dir in (*cleanup_dirs.values(), *rollback_dirs.values()):
                if _path_exists(cleanup_dir):
                    try:
                        self._remove_exact(cleanup_dir)
                    except (OSError, BackupError):
                        pass
            raise BackupError("restore_cleanup_failed") from error

    def _rollback_restore(
        self,
        moved_old: list[tuple[Path, Path]],
        installed: list[Path],
        quarantined: dict[Path, Path] | None = None,
        rollback_sources: dict[Path, Path] | None = None,
    ) -> None:
        quarantined = quarantined or {}
        rollback_sources = rollback_sources or {}
        try:
            for target in reversed(installed):
                if _path_exists(target):
                    self._remove_exact(target)
            for target, backup in reversed(moved_old):
                source = quarantined.get(target)
                if source is None or not _path_exists(source):
                    source = rollback_sources.get(target, backup)
                if not _path_exists(source):
                    raise BackupError("restore_cleanup_failed")
                if _path_exists(target):
                    self._remove_exact(target)
                os.replace(source, target)
        except (OSError, BackupError) as error:
            raise BackupError("restore_cleanup_failed") from error

    async def rebuild_derived_state(self) -> RebuildResult:
        """Rebuild SQLite Chunk/FTS5 and Qdrant from verified authoritative content."""

        if self.embedding_provider is None:
            raise BackupError("embedding_not_configured")
        rebuild_started = False
        try:
            async with self.session_factory() as session, session.begin():
                await self._validate_artifacts(session)
                published = list(
                    (
                        await session.execute(
                            select(KnowledgeItem).where(
                                KnowledgeItem.status == "published",
                                KnowledgeItem.deleted_at.is_(None),
                                KnowledgeItem.current_content_version_id.is_not(None),
                            )
                        )
                    ).scalars()
                )
                validated: list[tuple[KnowledgeItem, ContentVersion, str]] = []
                for item in published:
                    version_id = item.current_content_version_id
                    if version_id is None:
                        raise BackupError("rebuild_failed")
                    version = await session.get(ContentVersion, version_id)
                    binding_result = await session.execute(
                        select(NoteBinding).where(NoteBinding.knowledge_item_id == item.id)
                    )
                    binding = binding_result.scalar_one_or_none()
                    if version is None or version.knowledge_item_id != item.id or binding is None:
                        raise BackupError("vault_state_invalid")
                    relative_path = safe_relative_path(binding.relative_path)
                    managed_vault_root = self.settings.managed_vault_root
                    vault_root = self.settings.vault_root
                    if managed_vault_root is None or vault_root is None or relative_path is None:
                        raise BackupError("vault_state_invalid")
                    try:
                        note = ObsidianVault(vault_root, self.settings.managed_vault_dir).read(
                            relative_path
                        )
                    except (OSError, ValueError) as error:
                        raise BackupError("vault_state_invalid") from error
                    if (
                        note.zhiliu_id != item.id
                        or content_hash(note.body) != version.content_hash
                        or content_hash(note.body) != item.content_hash
                        or content_hash(version.body) != version.content_hash
                    ):
                        raise BackupError("vault_state_invalid")
                    validated.append((item, version, relative_path))

                rebuild_started = True
                self.vector_store.clear()
                await session.execute(delete(Chunk))
                await session.execute(text("DELETE FROM chunk_fts"))
                index = IndexService(self.vector_store, self.embedding_provider)
                chunk_count = 0
                for item, version, relative_path in validated:
                    chunks = await index.index_version(session, item, version, relative_path)
                    chunk_count += len(chunks)
                result = RebuildResult(len(validated), chunk_count)
            return result
        except BackupError:
            if rebuild_started:
                try:
                    self.vector_store.clear()
                except Exception:
                    pass
            raise
        except Exception as error:
            if rebuild_started:
                try:
                    self.vector_store.clear()
                except Exception:
                    pass
            raise BackupError("rebuild_failed") from error

    async def _validate_artifacts(self, session: AsyncSession) -> None:
        artifacts = list((await session.execute(select(SourceArtifact))).scalars())
        for artifact in artifacts:
            if artifact.cleanup_state == "deleted":
                continue
            safe_path = safe_relative_path(artifact.relative_path)
            if (
                safe_path is None
                or artifact.byte_size < 0
                or not self.artifacts.verify(safe_path, artifact.content_hash, artifact.byte_size)
            ):
                raise BackupError("artifact_state_invalid")
