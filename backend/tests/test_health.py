from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.core.errors as errors
import app.services.health as health_service
import app.main as main_module
from app.core.config import PROJECT_ROOT, Settings, sqlite_url_for
from app.main import create_app, normalize_request_id


def test_root_returns_service_metadata(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "running"


def test_built_frontend_is_served_without_hiding_api_metadata(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend_dist = tmp_path / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)
    (frontend_dist / "index.html").write_text(
        '<!doctype html><div id="root">知流台</div>',
        encoding="utf-8",
    )
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    app = create_app(settings, start_background=False, serve_frontend=True)

    with TestClient(app) as test_client:
        frontend_response = test_client.get("/")
        metadata_response = test_client.get("/api/meta")

    assert frontend_response.status_code == 200
    assert frontend_response.headers["content-type"].startswith("text/html")
    assert '<div id="root">' in frontend_response.text
    assert metadata_response.status_code == 200
    assert metadata_response.json()["status"] == "running"


def test_health_returns_final_architecture_components(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    keys = {component["key"] for component in body["components"]}
    assert keys == {
        "api",
        "sqlite",
        "qdrant",
        "artifact_storage",
        "obsidian",
        "obsidian_watcher",
        "model_providers",
        "ffmpeg",
    }
    assert response.headers["x-request-id"]


def test_sqlite_probe_uses_wal_and_foreign_keys(settings: Settings) -> None:
    component = health_service.probe_sqlite(settings)
    assert component.state == "healthy"
    assert "journal_mode=wal" in component.detail


def test_sqlite_probe_reports_unavailable_for_invalid_parent(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(parent_file / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
    )
    assert health_service.probe_sqlite(settings).state == "unavailable"


def test_qdrant_and_artifact_health(settings: Settings) -> None:
    assert health_service.probe_qdrant(settings).state == "healthy"
    artifact = health_service.probe_writable_directory(
        "artifact_storage", "Artifact Storage", settings.artifact_root, create=True
    )
    assert artifact.state == "healthy"


def test_artifact_probe_reports_unavailable_for_file(tmp_path: Path) -> None:
    root = tmp_path / "artifact-file"
    root.write_text("not a directory", encoding="utf-8")
    component = health_service.probe_writable_directory(
        "artifact_storage", "Artifact Storage", root, create=False
    )
    assert component.state == "unavailable"


def test_vault_not_configured_and_watcher_degraded(tmp_path: Path) -> None:
    unconfigured = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
    )
    assert health_service.probe_obsidian(unconfigured).state == "not_configured"
    configured = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db2.sqlite"),
        qdrant_path=tmp_path / "qdrant2",
        artifact_root=tmp_path / "artifacts2",
        vault_path=str(tmp_path),
    )
    from app.obsidian.state import watcher_state

    watcher_state.running = False
    assert health_service.probe_obsidian_watcher(configured).state == "degraded"


@pytest.mark.asyncio
async def test_model_not_configured(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
    )
    component = await health_service.probe_model_providers(settings)
    assert component.state == "not_configured"


@pytest.mark.asyncio
async def test_fastembed_model_reports_configured_without_loading_model(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        embedding_provider="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_dimensions=512,
        embedding_cache_path=tmp_path / "models",
    )
    component = await health_service.probe_model_providers(settings)
    assert component.state == "configured"
    assert "FastEmbed" in component.detail
    assert settings.embedding_cache_path.is_dir()


@pytest.mark.asyncio
async def test_unreachable_model_does_not_report_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class UnreachableClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "UnreachableClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def get(self, *_: object, **__: object) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(health_service.httpx, "AsyncClient", UnreachableClient)
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        chat_base_url="http://127.0.0.1:9/v1",
        chat_model="missing-model",
    )
    assert (await health_service.probe_model_providers(settings)).state == "degraded"


def test_artifact_root_is_resolved_from_repository_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, artifact_root="./data/artifacts")
    assert settings.artifact_root == (PROJECT_ROOT / "data" / "artifacts").resolve()
    assert settings.qdrant_path == (PROJECT_ROOT / "data" / "qdrant").resolve()
    assert settings.database_path == (PROJECT_ROOT / "data" / "zhiliutai.db").resolve()


def test_http_validation_and_internal_errors_keep_request_id(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_app = create_app(settings, start_background=False, serve_frontend=False)

    @test_app.get("/test-validation")
    async def validation_route(value: int) -> dict[str, int]:
        return {"value": value}

    @test_app.get("/test-crash")
    async def crash_route() -> None:
        raise RuntimeError("test crash")

    exception_events: list[str] = []

    class CaptureLogger:
        def exception(self, event: str, **_: object) -> None:
            exception_events.append(event)

        def info(self, *_: object, **__: object) -> None:
            pass

    monkeypatch.setattr(errors.structlog, "get_logger", lambda *_: CaptureLogger())
    with TestClient(test_app, raise_server_exceptions=False) as test_client:
        missing = test_client.get("/api/does-not-exist", headers={"X-Request-ID": "request-404"})
        invalid = test_client.get(
            "/test-validation",
            params={"value": "x"},
            headers={"X-Request-ID": "request-422"},
        )
        crashed = test_client.get("/test-crash", headers={"X-Request-ID": "request-500"})
    for response, expected in [
        (missing, "request-404"),
        (invalid, "request-422"),
        (crashed, "request-500"),
    ]:
        assert response.json()["error"]["request_id"] == expected
        assert response.headers["x-request-id"] == expected
    assert exception_events == ["unhandled_exception"]


def test_request_id_is_normalized() -> None:
    assert normalize_request_id("safe-id_123") == "safe-id_123"
    assert normalize_request_id("bad id") != "bad id"
    assert len(normalize_request_id("x" * 81)) == 32
