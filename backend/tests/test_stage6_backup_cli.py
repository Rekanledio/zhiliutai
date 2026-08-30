from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from conftest import migrate


def _load_cli():
    path = Path(__file__).parents[2] / "scripts" / "zhiliutai_backup.py"
    spec = importlib.util.spec_from_file_location("zhiliutai_backup_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arguments(cli, root: Path, archive: Path) -> list[str]:
    return [
        "--database",
        str(root / "business.sqlite"),
        "--checkpoint",
        str(root / "checkpoint.sqlite"),
        "--artifacts",
        str(root / "artifacts"),
        "--managed-vault-root",
        str(root / "vault" / "managed"),
        "--qdrant",
        str(root / "qdrant"),
        "--archive",
        str(archive),
    ]


def test_offline_cli_backup_and_restore_use_explicit_targets(tmp_path: Path, monkeypatch, capsys) -> None:
    cli = _load_cli()
    source = tmp_path / "source"
    source.mkdir()
    database = source / "business.sqlite"
    migrate(
        cli.Settings(
            _env_file=None,
            database_url=cli.sqlite_url_for(database),
            qdrant_path=source / "qdrant",
            artifact_root=source / "artifacts",
            workflow_checkpoint_path=source / "checkpoint.sqlite",
            vault_path=str(source / "vault"),
        ),
        monkeypatch,
    )
    (source / "vault" / "managed").mkdir(parents=True)
    archive = tmp_path / "backup.zip"

    assert cli.main(["backup", *_arguments(cli, source, archive)]) == 0
    backup_output = json.loads(capsys.readouterr().out)
    assert backup_output["command"] == "backup"
    assert archive.is_file()

    target = tmp_path / "target"
    assert cli.main(["restore", *_arguments(cli, target, archive)]) == 0
    restore_output = json.loads(capsys.readouterr().out)
    assert restore_output["command"] == "restore"
    with sqlite3.connect(target / "business.sqlite") as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0007_tags_and_review_suggestions",)


def test_offline_cli_errors_are_stable_and_do_not_echo_paths(tmp_path: Path, capsys) -> None:
    cli = _load_cli()
    secret_path = tmp_path / "TRACEBACK_SENTINEL" / "CREDENTIAL_SECRET"
    args = [
        "restore",
        *_arguments(cli, tmp_path / "target", tmp_path / "missing.zip"),
    ]
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "backup_archive_invalid"
    assert "TRACEBACK_SENTINEL" not in captured.err
    assert str(secret_path) not in captured.err
