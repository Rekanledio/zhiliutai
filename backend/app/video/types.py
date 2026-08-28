"""Typed, provider-neutral contracts for video-derived artifacts.

These models describe data that a future video workflow may exchange.  They do
not download media, invoke a model, or decide where an artifact is stored.
"""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


Milliseconds = Annotated[StrictInt, Field(ge=0)]
PositiveMilliseconds = Annotated[StrictInt, Field(gt=0)]


@dataclass(frozen=True)
class SubtitleTrack:
    """Provider-neutral subtitle bytes; never an API request payload."""

    content: bytes
    format: Literal["vtt", "srt", "json3", "unknown"] = "unknown"
    language: str | None = None
    is_automatic: bool = False
    source_url: str | None = None
    provider: str = "unknown"
    tool_version: str = "unknown"


_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    str_strip_whitespace=True,
)


def _validate_http_url(value: str) -> str:
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


class TimedSpan(BaseModel):
    """A non-empty interval on a video timeline.

    ``duration_ms`` is optional validation context.  It is useful when a
    provider validates an item in isolation; manifests also validate all
    nested spans against ``VideoSourceMetadata.duration_ms``.
    """

    model_config = _MODEL_CONFIG

    start_ms: Milliseconds
    end_ms: PositiveMilliseconds
    duration_ms: Milliseconds | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> TimedSpan:
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be less than end_ms")
        if self.duration_ms is not None and self.end_ms > self.duration_ms:
            raise ValueError("end_ms must not exceed duration_ms")
        return self

    def validate_against_duration(self, duration_ms: int | None) -> None:
        """Raise when this interval exceeds a known enclosing duration."""

        if duration_ms is not None and self.end_ms > duration_ms:
            raise ValueError("end_ms must not exceed the source duration")


class VideoSourceMetadata(BaseModel):
    """Non-secret metadata describing the source video."""

    model_config = _MODEL_CONFIG

    source_url: str = Field(
        min_length=1,
        max_length=2048,
        validation_alias=AliasChoices("source_url", "url"),
    )
    requested_url: str | None = Field(default=None, max_length=2048)
    final_url: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    duration_ms: Milliseconds | None = None
    media_type: str | None = Field(default=None, max_length=100)
    video_id: str | None = Field(default=None, max_length=300)
    uploader: str | None = Field(default=None, max_length=300)
    width_px: StrictInt | None = Field(default=None, ge=1)
    height_px: StrictInt | None = Field(default=None, ge=1)
    frame_rate: float | None = Field(default=None, ge=0)
    is_live: bool = False
    video_kind: Literal["interview", "podcast", "slideshow", "tutorial", "other"] = "other"
    source_content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    provider: str | None = Field(default=None, min_length=1, max_length=200)
    tool_version: str | None = Field(default=None, min_length=1, max_length=200)

    _source_url_is_safe = field_validator("source_url")(_validate_http_url)

    @field_validator("requested_url", "final_url")
    @classmethod
    def _optional_urls_are_safe(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value)


class TranscriptSegment(TimedSpan):
    """A provider-neutral ASR segment with timestamp evidence."""

    text: str = Field(min_length=1, max_length=100_000)
    language: str | None = Field(default=None, min_length=1, max_length=32)
    speaker: str | None = Field(default=None, min_length=1, max_length=200)
    confidence: float | None = Field(default=None, ge=0, le=1)


class Chapter(TimedSpan):
    """A human-readable chapter interval."""

    title: str = Field(min_length=1, max_length=500)


class Keyframe(TimedSpan):
    """A selected visual frame or short scene interval."""

    keyframe_id: str = Field(min_length=1, max_length=200)
    artifact_id: str | None = Field(default=None, min_length=1, max_length=36)
    relative_path: str | None = Field(default=None, min_length=1, max_length=500)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)


class VisualEvent(TimedSpan):
    """A timestamped, provider-neutral visual observation."""

    event_type: Literal["scene", "slide", "code", "ui", "speaker", "other"] = "other"
    summary: str = Field(min_length=1, max_length=10_000)
    keyframe_ids: list[str] = Field(default_factory=list, max_length=64)


class ProcessingManifest(BaseModel):
    """Rebuildable manifest for video processing outputs.

    It intentionally stores provider/model identifiers only; API keys and
    other credentials are not valid fields in this contract.
    """

    model_config = _MODEL_CONFIG

    manifest_version: str = Field(default="video-processing-v1", min_length=1, max_length=80)
    source_metadata: VideoSourceMetadata = Field(
        validation_alias=AliasChoices("source_metadata", "source")
    )
    transcript_segments: list[TranscriptSegment] = Field(
        default_factory=list, validation_alias=AliasChoices("transcript_segments", "transcript")
    )
    chapters: list[Chapter] = Field(default_factory=list, max_length=500)
    keyframes: list[Keyframe] = Field(default_factory=list, max_length=2_000)
    visual_events: list[VisualEvent] = Field(default_factory=list, max_length=2_000)
    asr_provider: str | None = Field(default=None, min_length=1, max_length=200)
    asr_model: str | None = Field(default=None, min_length=1, max_length=200)
    vision_provider: str | None = Field(default=None, min_length=1, max_length=200)
    vision_model: str | None = Field(default=None, min_length=1, max_length=200)
    ocr_provider: str | None = Field(default=None, min_length=1, max_length=200)
    ocr_model: str | None = Field(default=None, min_length=1, max_length=200)
    source_provider: str | None = Field(default=None, min_length=1, max_length=200)
    source_tool_version: str | None = Field(default=None, min_length=1, max_length=200)
    source_content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    media_artifact_id: str | None = Field(default=None, min_length=1, max_length=36)
    transcript_artifact_id: str | None = Field(default=None, min_length=1, max_length=36)
    subtitle_artifact_ids: list[str] = Field(default_factory=list, max_length=64)
    processing_status: Literal["subtitle_ready", "asr_complete", "asr_required"] = "subtitle_ready"
    created_at: datetime | None = None

    @model_validator(mode="after")
    def validate_nested_durations(self) -> ProcessingManifest:
        duration_ms = self.source_metadata.duration_ms
        for field_name in ("transcript_segments", "chapters", "keyframes", "visual_events"):
            for index, span in enumerate(getattr(self, field_name)):
                try:
                    span.validate_against_duration(duration_ms)
                except ValueError as error:
                    raise ValueError(f"{field_name}[{index}]: {error}") from error
        return self

    @property
    def source(self) -> VideoSourceMetadata:
        """Compatibility/readability alias for the manifest's source metadata."""

        return self.source_metadata
