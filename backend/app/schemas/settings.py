from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr


SettingsHealthState = Literal[
    "healthy", "degraded", "not_configured", "configured", "unavailable"
]

ProviderCapability = Literal["chat", "embedding", "asr", "vision", "reranker"]
ProviderKind = Literal[
    "openai-compatible",
    "fastembed",
    "faster-whisper",
    "sentence-transformers",
]


class ProviderSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    capability: ProviderCapability
    provider_kind: ProviderKind
    configured: StrictBool
    credential_configured: StrictBool
    model: StrictStr | None = Field(default=None, max_length=200)


class ProviderSettingsGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chat: ProviderSettingsResponse
    embedding: ProviderSettingsResponse
    asr: ProviderSettingsResponse
    vision: ProviderSettingsResponse
    reranker: ProviderSettingsResponse


class VaultSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    configured: StrictBool
    managed_directory: StrictStr | None = Field(default=None, max_length=200)
    watcher_running: StrictBool
    sync_state: Literal["watching", "stopped", "degraded", "not_configured"]


class RetrievalSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rag_query_max_chars: StrictInt = Field(ge=1)
    rrf_k: StrictInt = Field(ge=1)
    fts_limit: StrictInt = Field(ge=1)
    vector_limit: StrictInt = Field(ge=1)
    threshold: StrictFloat = Field(ge=0, le=1)
    confident_rank: StrictInt = Field(ge=1)
    rerank_limit: StrictInt = Field(ge=1)


class ChunkingSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    strategy: Literal["paragraph_then_fixed_width"]
    max_chars: StrictInt = Field(ge=1)


class VideoSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    retention_policy: Literal["permanent", "until_expiry", "delete_after_processing"]
    retention_days: StrictInt = Field(ge=0)
    max_bytes: StrictInt = Field(ge=1)
    max_duration_seconds: StrictInt = Field(ge=1)
    ffmpeg_state: SettingsHealthState


class MaintenanceSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    backup_available: StrictBool
    rescan_available: StrictBool
    rebuild_available: StrictBool
    configuration_hint: StrictStr
    restore_note: StrictStr


class SettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    local_only: StrictBool
    bind_host: Literal["127.0.0.1", "loopback", "non_loopback"]
    vault: VaultSettingsResponse
    providers: ProviderSettingsGroup
    retrieval: RetrievalSettingsResponse
    chunking: ChunkingSettingsResponse
    video: VideoSettingsResponse
    maintenance: MaintenanceSettingsResponse


class MaintenanceRequest(BaseModel):
    """Optional empty body for maintenance calls; paths and options are not accepted."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SettingsRescanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    changed: StrictInt = Field(ge=0)
    renamed: StrictInt = Field(ge=0)
    missing: StrictInt = Field(ge=0)
    conflicts: StrictInt = Field(ge=0)
    invalid: StrictInt = Field(ge=0)
    deferred: StrictInt = Field(ge=0)


class SettingsRebuildResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    published_items: StrictInt = Field(ge=0)
    chunks: StrictInt = Field(ge=0)


class SettingsBackupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    archive_id: StrictStr = Field(pattern=r"^backup-[0-9a-f]{32}$")
    created_at: StrictStr = Field(min_length=1, max_length=80)
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    config_key: Literal["BACKUP_ROOT"] = "BACKUP_ROOT"
