import hashlib
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.config import PROJECT_ROOT, Settings, get_settings, sqlite_url_for
from app.main import create_app
from app.providers.models import DraftResult


class FakeDraftProvider:
    async def create_draft(self, title: str, content: str) -> DraftResult:
        return DraftResult(
            title=title,
            body=content,
            summary="确定性测试摘要",
            suggested_tags=["测试", "阶段2"],
            prompt_version="fake-draft-v1",
        )


class FakeEmbeddingProvider:
    model = "fake-embedding"
    version = "fake-v1"
    dimensions = 8

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([(digest[index] / 127.5) - 1 for index in range(8)])
        return vectors


def migrate(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("QDRANT_PATH", str(settings.qdrant_path))
    get_settings.cache_clear()
    config = Config(str(PROJECT_ROOT / "backend" / "alembic.ini"))
    config.set_main_option(
        "script_location", str(PROJECT_ROOT / "backend" / "app" / "db" / "migrations")
    )
    command.upgrade(config, "head")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    vault = tmp_path / "vault"
    vault.mkdir()
    return Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "zhiliutai.db"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        vault_path=str(vault),
        embedding_dimensions=8,
        obsidian_watch_interval_seconds=0.05,
        health_check_timeout=0.05,
    )


@pytest.fixture
def client(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    migrate(settings, monkeypatch)
    app = create_app(
        settings,
        FakeDraftProvider(),
        FakeEmbeddingProvider(),
        start_background=True,
        serve_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def wait_for_job(
    client: TestClient, job_id: str, expected: str = "succeeded", timeout: float = 4
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        last = response.json()
        if last["state"] == expected:
            return last
        time.sleep(0.03)
    raise AssertionError(f"job did not reach {expected}: {last}")
