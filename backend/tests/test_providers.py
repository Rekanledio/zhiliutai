from pathlib import Path

import pytest

from app.core.config import Settings, sqlite_url_for
from app.providers.models import FastEmbedEmbeddingProvider


class FakeVector:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def tolist(self) -> list[float]:
        return self.values


class FakeFastEmbedModel:
    def __init__(self, *, model_name: str, cache_dir: str) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir

    def embed(self, texts: list[str]) -> list[FakeVector]:
        return [FakeVector([float(len(text)), 1.0]) for text in texts]


@pytest.mark.asyncio
async def test_fastembed_provider_loads_lazily_and_validates_dimensions(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=sqlite_url_for(tmp_path / "db.sqlite"),
        qdrant_path=tmp_path / "qdrant",
        artifact_root=tmp_path / "artifacts",
        embedding_provider="fastembed",
        embedding_model="fake-zh",
        embedding_dimensions=2,
        embedding_cache_path=tmp_path / "models",
    )
    provider = FastEmbedEmbeddingProvider(settings, FakeFastEmbedModel)

    assert not settings.embedding_cache_path.exists()
    assert await provider.embed(["中文", "test"]) == [[2.0, 1.0], [4.0, 1.0]]
    assert settings.embedding_cache_path.is_dir()
