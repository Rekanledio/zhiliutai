from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, sqlite_url_for
from app.main import create_app
from app.schemas.settings import MaintenanceRequest, SettingsResponse
from app.services.maintenance import MaintenanceBusyError, MaintenanceCoordinator
from app.services.settings import build_settings_response


def _settings_for_root(root: Path, **overrides: object) -> Settings:
    vault = root / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "database_url": sqlite_url_for(root / "business.sqlite"),
        "qdrant_path": root / "qdrant",
        "artifact_root": root / "artifacts",
        "workflow_checkpoint_path": root / "checkpoint.sqlite",
        "backup_root": root / "backups",
        "vault_path": str(vault),
        "embedding_dimensions": 8,
        "health_check_timeout": 0.05,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_settings_projection_is_strict_and_does_not_expose_secrets_or_paths(
    tmp_path: Path,
) -> None:
    api_key = "SYNTHETIC_API_KEY"
    auth_secret = "Authorization: Bearer SYNTHETIC_AUTH"
    cookie_secret = "Cookie: SYNTHETIC_COOKIE"
    traceback_secret = "TRACEBACK_SENTINEL"
    vault_path = tmp_path / "real-vault-secret"
    settings = _settings_for_root(
        tmp_path,
        chat_base_url="http://127.0.0.1:9001/v1?api_key=" + api_key,
        chat_model="synthetic-chat",
        chat_api_key=api_key,
        embedding_provider="fastembed",
        embedding_model="synthetic-embedding",
        asr_model=traceback_secret,
        vision_model=r"C:\Vault\secret-model",
        reranker_model="/tmp/secret-model",
        vault_path=str(vault_path),
    )

    response = build_settings_response(settings, embedding_provider=object())
    payload = response.model_dump(mode="json")
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert set(payload) == {
        "local_only",
        "bind_host",
        "vault",
        "providers",
        "retrieval",
        "chunking",
        "video",
        "maintenance",
    }
    assert payload["vault"] == {
        "configured": True,
        "managed_directory": "知流台",
        "watcher_running": False,
        "sync_state": "stopped",
    }
    assert payload["providers"]["embedding"]["model"] == "synthetic-embedding"
    assert payload["providers"]["chat"]["configured"] is False
    assert payload["providers"]["asr"]["model"] is None
    assert payload["providers"]["vision"]["model"] is None
    assert payload["providers"]["reranker"]["model"] is None
    assert payload["chunking"]["max_chars"] == 800
    assert payload["maintenance"]["rebuild_available"] is False
    for sentinel in (
        api_key,
        auth_secret,
        cookie_secret,
        traceback_secret,
        str(vault_path),
        r"C:\Vault\secret-model",
        "/tmp/secret-model",
        "api_key=",
    ):
        assert sentinel not in serialized

    with pytest.raises(ValidationError):
        SettingsResponse.model_validate({**payload, "unexpected": "value"})
    with pytest.raises(ValidationError):
        MaintenanceRequest.model_validate({"destination": r"C:\secret.zip"})


def test_settings_api_uses_server_generated_backup_root_and_rejects_body_injection(
    client,
    settings: Settings,
    tmp_path: Path,
) -> None:
    settings.backup_root = tmp_path / "server-backups"

    response = client.get("/api/settings")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload["providers"]) == {"chat", "embedding", "asr", "vision", "reranker"}
    assert "vault_path" not in json.dumps(payload, ensure_ascii=False)

    for endpoint in ("/api/settings/rescan", "/api/settings/rebuild", "/api/settings/backup"):
        rejected = client.post(endpoint, json={"destination": r"C:\client-selected.zip"})
        assert rejected.status_code == 422

    backup = None
    for _ in range(50):
        candidate = client.post("/api/settings/backup")
        if candidate.status_code != 409 or candidate.json()["error"]["code"] != "maintenance_busy":
            backup = candidate
            break
        time.sleep(0.02)
    assert backup is not None
    assert backup.status_code == 201
    backup_payload = backup.json()
    assert backup_payload["archive_id"].startswith("backup-")
    assert backup_payload["config_key"] == "BACKUP_ROOT"
    assert str(tmp_path) not in json.dumps(backup_payload, ensure_ascii=False)
    archives = list((tmp_path / "server-backups").glob("*.zip"))
    assert len(archives) == 1
    assert archives[0].name == backup_payload["archive_id"] + ".zip"


def test_settings_operations_report_missing_capabilities_without_fake_success(
    tmp_path: Path,
) -> None:
    settings = _settings_for_root(tmp_path, vault_path=None)
    app = create_app(settings, start_background=False, serve_frontend=False)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/api/settings").json()["maintenance"] == {
            "backup_available": False,
            "rescan_available": False,
            "rebuild_available": False,
            "configuration_hint": "配置通过项目根目录 .env，重启后生效；API Key 仅在后端秘密配置中使用。",
            "restore_note": "恢复必须先停止服务，再按文档化离线 CLI 执行；设置页不提供在线恢复。",
        }
        rescan = client.post("/api/settings/rescan")
        rebuild = client.post("/api/settings/rebuild")
        backup = client.post("/api/settings/backup")

    assert rescan.status_code == 409
    assert rescan.json()["error"]["code"] == "vault_not_configured"
    assert rebuild.status_code == 503
    assert rebuild.json()["error"]["code"] == "embedding_not_configured"
    assert backup.status_code == 409
    assert backup.json()["error"]["code"] == "vault_not_configured"


@pytest.mark.asyncio
async def test_cancelled_backup_waits_for_thread_before_releasing_both_locks(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class FakeStage2:
        mutation_lock = asyncio.Lock()

    class FakeBackup:
        def create_backup(self, _destination: Path) -> object:
            started.set()
            release.wait(timeout=5)
            return object()

    coordinator = MaintenanceCoordinator(FakeStage2(), FakeBackup())
    task = asyncio.create_task(coordinator.backup(tmp_path / "backup.zip"))
    await asyncio.to_thread(started.wait, 2)
    assert coordinator.lock.locked()
    assert FakeStage2.mutation_lock.locked()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert coordinator.lock.locked()
    assert FakeStage2.mutation_lock.locked()
    with pytest.raises(MaintenanceBusyError):
        await coordinator.rescan()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not coordinator.lock.locked()
    assert not FakeStage2.mutation_lock.locked()
