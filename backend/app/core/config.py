from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def sqlite_url_for(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


class Settings(BaseSettings):
    app_name: str = "知流台 API"
    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    database_url: str = "sqlite+aiosqlite:///data/zhiliutai.db"
    sqlite_busy_timeout_ms: int = 5000
    workflow_checkpoint_path: Path = PROJECT_ROOT / "data" / "checkpoints" / "workflows.db"
    qdrant_path: Path = PROJECT_ROOT / "data" / "qdrant"

    vault_path: str | None = None
    managed_vault_dir: str = "知流台"
    artifact_root: Path = PROJECT_ROOT / "data" / "artifacts"
    obsidian_watch_interval_seconds: float = 1.0
    source_max_bytes: int = 10_000_000
    source_fetch_timeout: float = 10.0
    source_max_redirects: int = 3
    video_max_bytes: int = 500_000_000
    video_max_duration_seconds: int = 4 * 60 * 60
    video_fetch_timeout: float = 60.0
    video_max_redirects: int = 3
    video_max_subtitle_bytes: int = 10_000_000
    video_max_subtitle_segments: int = 50_000
    video_max_audio_bytes: int = 100_000_000
    video_max_keyframes: int = 2_000
    video_media_retention_policy: Literal[
        "permanent", "until_expiry", "delete_after_processing"
    ] = "delete_after_processing"
    video_media_retention_days: int = 7
    video_ffmpeg_executable: str = "ffmpeg"
    video_ytdlp_executable: str = "yt-dlp"
    video_asr_fallback_enabled: bool = True

    chat_base_url: str | None = None
    chat_model: str | None = None
    chat_api_key: str | None = Field(default=None, repr=False)
    embedding_provider: Literal["openai-compatible", "fastembed"] = "openai-compatible"
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = Field(default=None, repr=False)
    embedding_dimensions: int = 1536
    embedding_cache_path: Path = PROJECT_ROOT / "data" / "models" / "fastembed"
    asr_base_url: str | None = None
    asr_model: str | None = None
    asr_api_key: str | None = Field(default=None, repr=False)
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_api_key: str | None = Field(default=None, repr=False)
    reranker_base_url: str | None = None
    reranker_model: str | None = None
    reranker_api_key: str | None = Field(default=None, repr=False)

    rag_query_max_chars: int = 2_000
    rag_rrf_k: int = 60
    rag_fts_limit: int = 30
    rag_vector_limit: int = 30
    rag_vector_score_threshold: float = 0.35
    rag_fts_confident_rank: int = 3
    rag_rerank_limit: int = 20

    health_check_timeout: float = 0.35
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator(
        "artifact_root",
        "qdrant_path",
        "embedding_cache_path",
        "workflow_checkpoint_path",
        mode="before",
    )
    @classmethod
    def resolve_data_path(cls, value: str | Path) -> Path:
        return resolve_project_path(value)

    @field_validator("database_url", mode="before")
    @classmethod
    def resolve_database_url(cls, value: str) -> str:
        prefix = "sqlite+aiosqlite:///"
        if not value.startswith(prefix):
            raise ValueError("DATABASE_URL must use sqlite+aiosqlite")
        raw_path = value.removeprefix(prefix)
        return sqlite_url_for(resolve_project_path(raw_path))

    @property
    def database_path(self) -> Path:
        return Path(self.database_url.removeprefix("sqlite+aiosqlite:///"))

    @property
    def vault_root(self) -> Path | None:
        if not self.vault_path:
            return None
        return Path(self.vault_path).expanduser().resolve()

    @property
    def managed_vault_root(self) -> Path | None:
        root = self.vault_root
        return None if root is None else (root / self.managed_vault_dir).resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
