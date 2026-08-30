from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.collections import normalize_collection_names
from app.schemas.tags import normalize_tag_names


class TextSourceRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    source_type: Literal["text", "markdown"] = "text"
    title: str | None = Field(default=None, max_length=300)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class UrlSourceRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=300)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class SubmissionResponse(BaseModel):
    item_id: str
    job_id: str
    deduplicated: bool


class ItemPatchRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    expected_content_hash: str | None = Field(default=None, min_length=64, max_length=64)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    approved: bool = True
    decision: Literal["approve", "reject", "cancel"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    summary: str | None = Field(default=None, max_length=20_000)
    suggested_tags: list[str] | None = Field(default=None, max_length=50)
    suggested_collections: list[str] | None = Field(default=None, max_length=50)

    @field_validator("suggested_tags", mode="before")
    @classmethod
    def _normalize_tag_values(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        return normalize_tag_names(value)

    @field_validator("suggested_collections", mode="before")
    @classmethod
    def _normalize_collection_values(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        return normalize_collection_names(value)

    def resolved_decision(self) -> Literal["approve", "reject", "cancel"]:
        if self.decision is not None:
            return self.decision
        return "approve" if self.approved else "reject"


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["approve", "reject", "cancel"] = "approve"

    def resolved_decision(self) -> Literal["approve", "reject", "cancel"]:
        return self.decision


class ItemResponse(BaseModel):
    id: str
    title: str
    source_type: str
    status: str
    content_hash: str
    current_content_version_id: str | None = None
    pending_content_version_id: str | None = None
    has_pending_review: bool = False
    body: str | None = None
    summary: str | None = None
    suggested_tags: list[str] = Field(default_factory=list)
    suggested_collections: list[str] = Field(default_factory=list)
    confirmed_tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    source_metadata: dict[str, object] | None = None
    version_no: int | None = None
    note_relative_path: str | None = None
    sync_state: str | None = None
    created_at: datetime
    updated_at: datetime


class JobAttemptResponse(BaseModel):
    id: str
    attempt_no: int
    state: str
    stage: str
    started_at: datetime
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    error: dict[str, object] | None = None


class JobResponse(BaseModel):
    id: str
    kind: str
    state: str
    stage: str
    progress: float
    retry_count: int
    max_retries: int
    error: dict[str, object] | None = None
    result: dict[str, object] | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    attempts: list[JobAttemptResponse] = Field(default_factory=list)


class ObsidianStatusResponse(BaseModel):
    configured: bool
    watcher_running: bool
    managed_directory: str | None = None
    last_heartbeat_at: datetime | None = None
    last_error: str | None = None


class RescanResponse(BaseModel):
    changed: int
    renamed: int
    missing: int
    conflicts: int
    invalid: int
    deferred: int = 0


class ObsidianOpenResponse(BaseModel):
    uri: str
