from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
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
    qdrant_path: Path = PROJECT_ROOT / "data" / "qdrant"

    vault_path: str | None = None
    managed_vault_dir: str = "知流台"
    artifact_root: Path = PROJECT_ROOT / "data" / "artifacts"
    obsidian_watch_interval_seconds: float = 1.0

    chat_base_url: str | None = None
    chat_model: str | None = None
    chat_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: str | None = None
    embedding_dimensions: int = 1536
    asr_base_url: str | None = None
    asr_model: str | None = None
    vision_base_url: str | None = None
    vision_model: str | None = None
    reranker_base_url: str | None = None
    reranker_model: str | None = None

    health_check_timeout: float = 0.35
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("artifact_root", "qdrant_path", mode="before")
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
