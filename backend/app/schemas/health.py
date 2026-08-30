from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

HealthState = Literal[
    "healthy", "degraded", "not_configured", "configured", "unavailable"
]


class HealthComponent(BaseModel):
    key: str
    label: str
    state: HealthState
    detail: str
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    checked_at: datetime
    components: list[HealthComponent]


class DashboardStats(BaseModel):
    knowledge_count: int = Field(ge=0)
    today_added: int = Field(ge=0)
    pending_review: int = Field(ge=0)
    processing: int = Field(ge=0)


class DashboardPendingReview(BaseModel):
    id: str
    title: str
    source_type: str
    status: str
    updated_at: datetime


class DashboardRecentItem(BaseModel):
    id: str
    title: str
    source_type: str
    status: str
    updated_at: datetime


class DashboardJob(BaseModel):
    id: str
    kind: str
    state: str
    stage: str
    progress: float = Field(ge=0, le=1)
    heartbeat_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    error: dict[str, object] | None = None


class DashboardResponse(BaseModel):
    greeting: str
    date_label: str
    stats: DashboardStats
    health: HealthResponse
    pending_reviews: list[DashboardPendingReview]
    recent_items: list[DashboardRecentItem]
    processing_jobs: list[DashboardJob]
