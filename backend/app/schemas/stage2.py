from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TextSourceRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    source_type: Literal["text", "markdown"] = "text"
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
    approved: bool = True


class ItemResponse(BaseModel):
    id: str
    title: str
    source_type: str
    status: str
    content_hash: str
    body: str | None = None
    summary: str | None = None
    suggested_tags: list[str] = []
    version_no: int | None = None
    note_relative_path: str | None = None
    sync_state: str | None = None
    created_at: datetime
    updated_at: datetime


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


class ObsidianOpenResponse(BaseModel):
    uri: str
