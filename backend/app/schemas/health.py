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


class DashboardResponse(BaseModel):
    greeting: str
    date_label: str
    stats: DashboardStats
    health: HealthResponse
    pending_reviews: list[dict[str, str]]
    recent_items: list[dict[str, str]]
    processing_jobs: list[dict[str, str]]
