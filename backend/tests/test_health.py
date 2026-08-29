from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.core.errors as errors
import app.services.health as health_service
import app.services.settings as settings_service
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


def test_ffmpeg_probe_defaults_to_ffmpeg_and_hides_missing_configuration() -> None:
    calls: list[str] = []

    def missing(executable: str) -> None:
        calls.append(executable)
        return None

    component = health_service.probe_ffmpeg(which=missing)

    assert calls == ["ffmpeg"]
    assert component.state == "not_configured"
    assert "ffmpeg" not in component.detail


@pytest.mark.asyncio
async def test_ffmpeg_probe_and_settings_use_configured_executable_without_leaking_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    configured_executable = "project-ffmpeg"
    resolved_path = tmp_path / "private" / "ffmpeg"

    def resolve(executable: str) -> str:
        calls.append(executable)
        return str(resolved_path)

    monkeypatch.setattr(health_service.shutil, "which", resolve)
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        video_ffmpeg_executable=configured_executable,
    )

    health_report = await health_service.build_health_report(settings)
    health_component = next(
        component for component in health_report.components if component.key == "ffmpeg"
    )
    settings_response = settings_service.build_settings_response(settings)
    serialized = repr(settings_response.model_dump(mode="json"))

    assert calls == [configured_executable, configured_executable]
    assert health_component.state == "healthy"
    assert settings_response.video.ffmpeg_state == "healthy"
    assert str(resolved_path) not in health_component.detail
    assert str(resolved_path) not in serialized
    assert configured_executable not in health_component.detail


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


def _model_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": sqlite_url_for(tmp_path / "db.sqlite"),
        "qdrant_path": tmp_path / "qdrant",
        "artifact_root": tmp_path / "artifacts",
        "chat_base_url": "http://127.0.0.1:9001/v1",
        "chat_model": "synthetic-chat",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _install_model_mock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int = 200,
    error: Exception | None = None,
) -> tuple[list[httpx.Request], list[dict[str, object]]]:
    requests: list[httpx.Request] = []
    client_options: list[dict[str, object]] = []
    real_client = health_service.httpx.AsyncClient
    response_secret = "SYNTHETIC_HEALTH_RESPONSE_SECRET"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if error is not None:
            raise error
        return httpx.Response(
            status_code,
            text=f"Authorization: Bearer {response_secret}; Cookie: {response_secret}",
            headers={"Set-Cookie": response_secret},
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client_options.append(kwargs)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(health_service.httpx, "AsyncClient", client_factory)
    return requests, client_options


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
    assert "远程能力" in component.detail
    assert "Chat=未配置" in component.detail
    assert settings.embedding_cache_path.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_state", "expected_detail"),
    [
        (200, "healthy", "已验证"),
        (401, "degraded", "认证未通过"),
        (403, "degraded", "认证未通过"),
        (404, "degraded", "客户端错误"),
        (500, "degraded", "服务端错误"),
    ],
)
async def test_model_probe_only_treats_successful_2xx_as_healthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_code: int,
    expected_state: str,
    expected_detail: str,
) -> None:
    requests, client_options = _install_model_mock(
        monkeypatch,
        status_code=status_code,
    )
    component = await health_service.probe_model_providers(
        _model_settings(tmp_path, chat_api_key="SYNTHETIC_API_KEY")
    )

    assert component.state == expected_state
    assert expected_detail in component.detail
    assert f"HTTP {status_code}" in component.detail
    assert "SYNTHETIC_API_KEY" not in component.detail
    assert "SYNTHETIC_HEALTH_RESPONSE_SECRET" not in component.detail
    assert requests[0].url == "http://127.0.0.1:9001/v1/models"
    assert client_options[0]["follow_redirects"] is False
    assert client_options[0]["trust_env"] is False


@pytest.mark.asyncio
async def test_model_probe_rejects_redirect_without_following_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests, _ = _install_model_mock(monkeypatch, status_code=302)

    component = await health_service.probe_model_providers(_model_settings(tmp_path))

    assert component.state == "degraded"
    assert "重定向被拒绝" in component.detail
    assert "HTTP 302" in component.detail
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_detail"),
    [
        (httpx.ReadTimeout("timeout sentinel"), "探针超时"),
        (httpx.ConnectError("connection sentinel"), "服务不可达"),
    ],
)
async def test_model_probe_reports_stable_network_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
    expected_detail: str,
) -> None:
    _install_model_mock(monkeypatch, error=error)

    component = await health_service.probe_model_providers(_model_settings(tmp_path))

    assert component.state == "degraded"
    assert expected_detail in component.detail
    assert "sentinel" not in component.detail


@pytest.mark.asyncio
async def test_loopback_provider_without_api_key_can_be_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests, _ = _install_model_mock(monkeypatch)

    component = await health_service.probe_model_providers(
        _model_settings(tmp_path, chat_api_key=None)
    )

    assert component.state == "healthy"
    assert "本地端点已验证" in component.detail
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_fastembed_and_remote_capabilities_are_explicitly_aggregated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests, _ = _install_model_mock(monkeypatch)
    settings = _model_settings(
        tmp_path,
        embedding_provider="fastembed",
        embedding_model="synthetic-fastembed",
        embedding_cache_path=tmp_path / "models",
        vision_base_url="http://127.0.0.1:9002/v1",
        vision_model="synthetic-vision",
    )

    component = await health_service.probe_model_providers(settings)

    assert component.state == "configured"
    assert len(requests) == 2
    assert "本地能力：Embedding=FastEmbed" in component.detail
    assert "Chat 本地端点已验证" in component.detail
    assert "Vision 本地端点已验证" in component.detail
    assert "ASR=未配置" in component.detail
    assert "Reranker=未配置" in component.detail


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


@pytest.mark.asyncio
async def test_asr_and_vision_health_probe_uses_keys_without_exposing_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class CaptureClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "CaptureClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def get(self, url: str, *, headers: dict[str, str]) -> httpx.Response:
            calls.append((url, headers))
            return httpx.Response(200)

    monkeypatch.setattr(health_service.httpx, "AsyncClient", CaptureClient)
    asr_key = "synthetic-asr-key"
    vision_key = "synthetic-vision-key"
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        asr_base_url="http://127.0.0.1:9001/v1",
        asr_model="fake-asr",
        asr_api_key=asr_key,
        vision_base_url="http://127.0.0.1:9002/v1",
        vision_model="fake-vision",
        vision_api_key=vision_key,
    )

    component = await health_service.probe_model_providers(settings)

    assert component.state == "healthy"
    assert calls == [
        (
            "http://127.0.0.1:9001/v1/models",
            {"Authorization": f"Bearer {asr_key}"},
        ),
        (
            "http://127.0.0.1:9002/v1/models",
            {"Authorization": f"Bearer {vision_key}"},
        ),
    ]
    assert asr_key not in component.detail
    assert vision_key not in component.detail
    assert asr_key not in repr(settings)
    assert vision_key not in repr(settings)


@pytest.mark.asyncio
async def test_health_rejects_model_endpoint_credentials_and_query_strings(
    tmp_path: Path,
) -> None:
    secret = "synthetic-query-secret"
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        asr_base_url=f"http://127.0.0.1:9001/v1?api_key={secret}",
        asr_model="fake-asr",
    )

    component = await health_service.probe_model_providers(settings)

    assert component.state == "degraded"
    assert secret not in component.detail


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
        def error(self, event: str, **_: object) -> None:
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
