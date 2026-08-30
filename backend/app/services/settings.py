from __future__ import annotations

import ipaddress
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings
from app.core.paths import safe_relative_path
from app.core.safety import redact_sensitive_text
from app.obsidian.state import watcher_state
from app.schemas.settings import (
    ChunkingSettingsResponse,
    MaintenanceSettingsResponse,
    ProviderSettingsGroup,
    ProviderSettingsResponse,
    RetrievalSettingsResponse,
    SettingsResponse,
    VideoSettingsResponse,
    VaultSettingsResponse,
)
from app.services.content import DEFAULT_CHUNK_MAX_CHARS
from app.services.health import probe_ffmpeg

_UNSAFE_IDENTIFIER = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|cookie|set[-_ ]?cookie|bearer|"
    r"access[_ -]?token|refresh[_ -]?token|password|secret|traceback|stack\s+trace)"
)


def _safe_model_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        or _UNSAFE_IDENTIFIER.search(normalized)
        or redact_sensitive_text(normalized) != normalized
        or "://" in normalized
        or "?" in normalized
    ):
        return None
    windows = PureWindowsPath(normalized)
    posix = PurePosixPath(normalized)
    if windows.is_absolute() or windows.drive or windows.root or posix.is_absolute():
        return None
    return normalized


def _safe_endpoint(value: object) -> bool:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.netloc
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and redact_sensitive_text(value) == value
    )


def _provider(
    capability: str,
    provider_kind: str,
    *,
    model: object,
    base_url: object | None = None,
    api_key: object | None = None,
) -> ProviderSettingsResponse:
    safe_model = _safe_model_identifier(model)
    configured = safe_model is not None and (
        provider_kind in {"fastembed", "faster-whisper", "sentence-transformers"}
        or _safe_endpoint(base_url)
    )
    return ProviderSettingsResponse(
        capability=capability,  # type: ignore[arg-type]
        provider_kind=provider_kind,  # type: ignore[arg-type]
        configured=configured,
        credential_configured=isinstance(api_key, str) and bool(api_key),
        model=safe_model,
    )


def _is_loopback(value: str) -> bool:
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _database_exists(settings: Settings) -> bool:
    try:
        return settings.database_path.is_file()
    except OSError:
        return False


def build_settings_response(
    settings: Settings,
    *,
    embedding_provider: Any | None = None,
) -> SettingsResponse:
    managed_directory = safe_relative_path(settings.managed_vault_dir)
    vault_configured = settings.vault_root is not None and managed_directory is not None
    watcher_running = bool(vault_configured and watcher_state.running)
    sync_state = (
        "not_configured"
        if not vault_configured
        else "watching"
        if watcher_running
        else "degraded"
        if watcher_state.last_error
        else "stopped"
    )
    local_only = _is_loopback(settings.api_host)
    bind_host = (
        "127.0.0.1"
        if settings.api_host == "127.0.0.1"
        else "loopback"
        if local_only
        else "non_loopback"
    )
    database_exists = _database_exists(settings)
    return SettingsResponse(
        local_only=local_only,
        bind_host=bind_host,
        vault=VaultSettingsResponse(
            configured=vault_configured,
            managed_directory=managed_directory if vault_configured else None,
            watcher_running=watcher_running,
            sync_state=sync_state,
        ),
        providers=ProviderSettingsGroup(
            chat=_provider(
                "chat",
                "openai-compatible",
                model=settings.chat_model,
                base_url=settings.chat_base_url,
                api_key=settings.chat_api_key,
            ),
            embedding=_provider(
                "embedding",
                settings.embedding_provider,
                model=settings.embedding_model,
                base_url=settings.embedding_base_url,
                api_key=settings.embedding_api_key,
            ),
            asr=_provider(
                "asr",
                settings.asr_provider,
                model=settings.asr_model,
                base_url=settings.asr_base_url,
                api_key=settings.asr_api_key,
            ),
            vision=_provider(
                "vision",
                settings.vision_provider,
                model=settings.vision_model,
                base_url=settings.vision_base_url,
                api_key=settings.vision_api_key,
            ),
            reranker=_provider(
                "reranker",
                settings.reranker_provider,
                model=settings.reranker_model,
                base_url=settings.reranker_base_url,
                api_key=settings.reranker_api_key,
            ),
        ),
        retrieval=RetrievalSettingsResponse(
            rag_query_max_chars=settings.rag_query_max_chars,
            rrf_k=settings.rag_rrf_k,
            fts_limit=settings.rag_fts_limit,
            vector_limit=settings.rag_vector_limit,
            threshold=float(settings.rag_vector_score_threshold),
            confident_rank=settings.rag_fts_confident_rank,
            rerank_limit=settings.rag_rerank_limit,
        ),
        chunking=ChunkingSettingsResponse(
            strategy="paragraph_then_fixed_width",
            max_chars=DEFAULT_CHUNK_MAX_CHARS,
        ),
        video=VideoSettingsResponse(
            retention_policy=settings.video_media_retention_policy,
            retention_days=settings.video_media_retention_days,
            max_bytes=settings.video_max_bytes,
            max_duration_seconds=settings.video_max_duration_seconds,
            ffmpeg_state=probe_ffmpeg(
                executable=settings.video_ffmpeg_executable
            ).state,
        ),
        maintenance=MaintenanceSettingsResponse(
            backup_available=bool(vault_configured and database_exists),
            rescan_available=vault_configured,
            rebuild_available=bool(vault_configured and database_exists and embedding_provider),
            configuration_hint="配置通过项目根目录 .env，重启后生效；API Key 仅在后端秘密配置中使用。",
            restore_note="恢复必须先停止服务，再按文档化离线 CLI 执行；设置页不提供在线恢复。",
        ),
    )
