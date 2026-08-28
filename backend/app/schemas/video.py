"""API-facing contracts for the minimal video source route.

The request model is intentionally narrower than the provider boundary:
callers can submit only a public video URL and approved processing options.
Domain result models are re-exported here so callers have one obvious schema
import path.
"""

from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.video.types import (
    Chapter,
    Keyframe,
    ProcessingManifest,
    TimedSpan,
    TranscriptSegment,
    VideoSourceMetadata,
    VisualEvent,
)


def _validate_video_url(value: str) -> str:
    parsed = urlsplit(value)
    if value != value.strip() or parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("视频来源 URL 必须使用 http 或 https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("视频来源 URL 不得包含用户凭据")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("视频来源 URL 端口无效") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("视频来源 URL 端口无效")
    sensitive_keys = {
        "api_key",
        "apikey",
        "key",
        "access_token",
        "auth",
        "authorization",
        "password",
        "secret",
        "token",
    }
    if any(
        key.casefold().replace("-", "_") in sensitive_keys
        for raw in (parsed.query, parsed.fragment)
        for key, _ in parse_qsl(raw, keep_blank_values=True)
    ):
        raise ValueError("视频来源 URL 不得携带敏感认证参数")
    return value


class VideoSourceRequest(BaseModel):
    """Validated input contract for ``POST /api/sources/video``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(
        min_length=1,
        max_length=2048,
    )
    title: str | None = Field(default=None, max_length=300)
    language: str | None = Field(default=None, min_length=1, max_length=32)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    enable_vision: bool = False

    _url_is_safe = field_validator("url")(_validate_video_url)

    @model_validator(mode="before")
    @classmethod
    def _accept_source_url_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "source_url" not in value:
            return value
        if "url" in value:
            return value
        copied = dict(value)
        copied["url"] = copied.pop("source_url")
        return copied

    @property
    def source_url(self) -> str:
        return self.url


__all__ = [
    "Chapter",
    "Keyframe",
    "ProcessingManifest",
    "TimedSpan",
    "TranscriptSegment",
    "VideoSourceMetadata",
    "VideoSourceRequest",
    "VisualEvent",
]
