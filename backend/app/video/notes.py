"""Deterministic transcript, chapter and layered-note helpers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import unescape

from app.services.content import normalize_content
from app.video.subtitles import format_timestamp
from app.video.types import Chapter, TranscriptSegment, VisualEvent


class VideoTextError(ValueError):
    """Raised when provider text cannot safely become knowledge content."""


_UNSAFE_TEXT = re.compile(
    r"(?is)<\s*(?:script|iframe|object|embed|svg|math)\b|"
    r"javascript\s*:|data\s*:\s*text/html|\bon[a-z0-9_-]+\s*="
)


def validate_transcript_segments(
    segments: Sequence[TranscriptSegment],
    *,
    duration_ms: int | None = None,
    max_segments: int = 50_000,
) -> list[TranscriptSegment]:
    if len(segments) > max_segments:
        raise VideoTextError("转录段数量超过限制")
    result: list[TranscriptSegment] = []
    previous_start = -1
    for index, segment in enumerate(segments):
        segment.validate_against_duration(duration_ms)
        if segment.start_ms < previous_start:
            raise VideoTextError(f"转录段顺序无效：第 {index + 1} 段乱序")
        if not segment.text.strip() or "\x00" in segment.text:
            raise VideoTextError("转录文本为空或包含 NUL 字符")
        if any(ord(char) < 32 and char not in {"\t", "\n", "\r"} for char in segment.text):
            raise VideoTextError("转录文本包含控制字符")
        if _UNSAFE_TEXT.search(unescape(segment.text)):
            raise VideoTextError("转录文本包含不安全内容")
        result.append(segment)
        previous_start = segment.start_ms
    return result


def validate_video_alignment(
    segments: Sequence[TranscriptSegment],
    visual_events: Sequence[VisualEvent],
    *,
    duration_ms: int | None = None,
) -> None:
    """Validate that audio and visual evidence share one millisecond timeline."""

    validate_transcript_segments(segments, duration_ms=duration_ms)
    previous_start = -1
    for index, event in enumerate(visual_events):
        try:
            event.validate_against_duration(duration_ms)
        except ValueError as error:
            raise VideoTextError(f"视觉事件[{index}]超出视频时间轴") from error
        if event.start_ms < previous_start:
            raise VideoTextError(f"视觉事件顺序无效：第 {index + 1} 段乱序")
        if any(
            ord(char) < 32 and char not in {"\t", "\n", "\r"}
            for char in event.summary
        ) or _UNSAFE_TEXT.search(unescape(event.summary)):
            raise VideoTextError("视觉事件文本不安全")
        previous_start = event.start_ms


def build_chapters(
    segments: Sequence[TranscriptSegment],
    *,
    gap_ms: int = 120_000,
    max_chapters: int = 500,
) -> list[Chapter]:
    if not segments:
        return []
    chapters: list[Chapter] = []
    group_start = segments[0].start_ms
    group_end = segments[0].end_ms
    title = segments[0].text[:80].strip() or "未命名章节"
    for segment in segments[1:]:
        if segment.start_ms - group_end >= gap_ms:
            chapters.append(Chapter(start_ms=group_start, end_ms=group_end, title=title))
            group_start = segment.start_ms
            title = segment.text[:80].strip() or "未命名章节"
        group_end = max(group_end, segment.end_ms)
    chapters.append(Chapter(start_ms=group_start, end_ms=group_end, title=title))
    if len(chapters) > max_chapters:
        raise VideoTextError("章节数量超过限制")
    return chapters


def _segment_display(segment: TranscriptSegment) -> str:
    return f"[{format_timestamp(segment.start_ms)}] {segment.text}"


def render_layered_note(
    segments: Sequence[TranscriptSegment],
    chapters: Sequence[Chapter],
    visual_events: Sequence[VisualEvent],
    *,
    transcript_artifact_id: str,
) -> tuple[str, list[dict[str, object]]]:
    """Return note body and source segments whose text hashes match that body."""

    entries: list[tuple[str, dict[str, object]]] = [
        ("## 字幕转录", {"kind": "video_section", "section": "transcript"})
    ]
    for segment in segments:
        entries.append(
            (
                _segment_display(segment),
                {
                    "kind": "video",
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "language": segment.language,
                    "transcript_artifact_id": transcript_artifact_id,
                },
            )
        )
    if chapters:
        entries.append(("## 章节", {"kind": "video_section", "section": "chapters"}))
        for chapter in chapters:
            entries.append(
                (
                    f"- [{format_timestamp(chapter.start_ms)}–{format_timestamp(chapter.end_ms)}] "
                    f"{chapter.title}",
                    {
                        "kind": "video_chapter",
                        "start_ms": chapter.start_ms,
                        "end_ms": chapter.end_ms,
                    },
                )
            )
    if visual_events:
        entries.append(("## 视觉观察", {"kind": "video_section", "section": "visual"}))
        for event in visual_events:
            entries.append(
                (
                    f"[{format_timestamp(event.start_ms)}] 视觉观察：{event.summary}",
                    {
                        "kind": "video_keyframe",
                        "start_ms": event.start_ms,
                        "end_ms": event.end_ms,
                        "event_type": event.event_type,
                        "keyframe_ids": list(event.keyframe_ids),
                    },
                )
            )
    body = normalize_content("\n\n".join(text for text, _ in entries))
    metadata_segments = [
        {"text": text, "locator": locator} for text, locator in entries
    ]
    return body, metadata_segments


__all__ = [
    "VideoTextError",
    "build_chapters",
    "render_layered_note",
    "validate_transcript_segments",
    "validate_video_alignment",
]
