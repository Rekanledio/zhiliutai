"""Offline backup, restore, and derived-index rebuild entry point.

This wrapper deliberately accepts every data destination explicitly.  Restore
is only performed by this short-lived process after the application, job
runner, watcher, and workflow checkpoint users have been stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from app.core.config import Settings, sqlite_url_for  # noqa: E402
from app.db.session import create_engine  # noqa: E402
from app.providers.models import (  # noqa: E402
    FastEmbedEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    ProviderNotConfigured,
)
from app.services.backup import (  # noqa: E402
    BackupError,
    BackupRestoreService,
    RestoreTargets,
)


def _add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", required=True, help="显式业务 SQLite 文件绝对路径")
    parser.add_argument("--checkpoint", required=True, help="显式 Graph checkpoint 文件绝对路径")
    parser.add_argument("--artifacts", required=True, help="显式 Artifact 根目录绝对路径")
    parser.add_argument(
        "--managed-vault-root",
        required=True,
        help="显式受管 Obsidian Markdown 根目录绝对路径",
    )
    parser.add_argument("--qdrant", required=True, help="显式 Qdrant Local 目录绝对路径")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="知流台离线 backup/restore/rebuild；restore 前必须停止所有运行时。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="创建一致性归档")
    _add_paths(backup)
    backup.add_argument("--archive", required=True, help="显式归档文件绝对路径")
    backup.add_argument("--overwrite", action="store_true", help="显式允许覆盖归档")

    restore = subparsers.add_parser("restore", help="离线恢复一致性归档")
    _add_paths(restore)
    restore.add_argument("--archive", required=True, help="显式归档文件绝对路径")
    restore.add_argument("--overwrite", action="store_true", help="显式允许覆盖恢复目标")

    rebuild = subparsers.add_parser("rebuild", help="从权威 Markdown/关系数据重建派生索引")
    _add_paths(rebuild)
    return parser


def _explicit_path(value: str, *, archive: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise BackupError("archive_invalid" if archive else "invalid_target")
    return path


def _settings(namespace: argparse.Namespace) -> Settings:
    managed_vault = _explicit_path(namespace.managed_vault_root)
    if managed_vault.name in {"", ".", ".."}:
        raise BackupError("invalid_target")
    return Settings(
        database_url=sqlite_url_for(_explicit_path(namespace.database)),
        workflow_checkpoint_path=_explicit_path(namespace.checkpoint),
        artifact_root=_explicit_path(namespace.artifacts),
        qdrant_path=_explicit_path(namespace.qdrant),
        vault_path=str(managed_vault.parent),
        managed_vault_dir=managed_vault.name,
    )


def _targets(namespace: argparse.Namespace) -> RestoreTargets:
    return RestoreTargets(
        _explicit_path(namespace.database),
        _explicit_path(namespace.checkpoint),
        _explicit_path(namespace.artifacts),
        _explicit_path(namespace.managed_vault_root),
    )


def _service_without_engine(settings: Settings) -> BackupRestoreService:
    # backup/restore are synchronous filesystem operations and deliberately do
    # not open a business or checkpoint connection in this process.
    return BackupRestoreService(settings, None)  # type: ignore[arg-type]


async def _rebuild(settings: Settings) -> dict[str, int]:
    engine = create_engine(settings)
    try:
        try:
            if settings.embedding_provider == "fastembed":
                embedding_provider = FastEmbedEmbeddingProvider(settings)
            else:
                embedding_provider = OpenAICompatibleEmbeddingProvider(settings)
        except (ProviderNotConfigured, ImportError) as error:
            raise BackupError("embedding_not_configured") from error
        service = BackupRestoreService(
            settings,
            async_sessionmaker(engine, expire_on_commit=False),
            embedding_provider,
        )
        result = await service.rebuild_derived_state()
        return {"published_items": result.published_items, "chunks": result.chunks}
    finally:
        await engine.dispose()


def _run(namespace: argparse.Namespace) -> dict[str, object]:
    settings = _settings(namespace)
    if namespace.command == "backup":
        result = _service_without_engine(settings).create_backup(
            _explicit_path(namespace.archive, archive=True),
            allow_overwrite=namespace.overwrite,
        )
        return {
            "command": "backup",
            "archive_sha256": result.archive_sha256,
            "files": len(result.manifest.files),
        }
    if namespace.command == "restore":
        result = _service_without_engine(settings).restore_backup(
            _explicit_path(namespace.archive, archive=True),
            _targets(namespace),
            allow_overwrite=namespace.overwrite,
            offline=True,
        )
        return {
            "command": "restore",
            "checkpoint_restored": result.checkpoint_restored,
            "files_restored": result.files_restored,
        }
    return {"command": "rebuild", **asyncio.run(_rebuild(settings))}


def main(argv: list[str] | None = None) -> int:
    namespace = _build_parser().parse_args(argv)
    try:
        print(json.dumps(_run(namespace), ensure_ascii=False, sort_keys=True))
    except BackupError as error:
        print(f"backup_{error.code}", file=sys.stderr)
        return 2
    except Exception:
        print("backup_failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
