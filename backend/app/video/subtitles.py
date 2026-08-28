"""Strict, offline subtitle normalization for the video ingestion pipeline."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from app.video.types import SubtitleTrack, TranscriptSegment


class SubtitleParseError(ValueError):
    """Raised when a subtitle track is malformed or unsafe to ingest."""


_TIMING = re.compile(r"^\s*(?P<start>[^ ]+)\s+-->\s+(?P<end>[^ ]+)(?:\s+.*)?$")
_SUSPICIOUS = re.compile(
    r"(?is)<\s*(?:script|iframe|object|embed|svg|math)\b|"
    r"javascript\s*:|data\s*:\s*text/html|\bon[a-z0-9_-]+\s*="
)
_ALLOWED_TAG = re.compile(r"</?(?:b|i|u|em|strong|c|v|ruby|rt)(?:\s+[^>]*)?>", re.IGNORECASE)


def parse_timestamp(value: str) -> int:
    """Parse a WebVTT/SRT timestamp into non-negative integer milliseconds."""

    raw = value.strip()
    parts = raw.split(":")
    if len(parts) not in {2, 3}:
        raise SubtitleParseError("字幕时间戳格式无效")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds_text = parts[2]
        else:
            hours = 0
            minutes = int(parts[0])
            seconds_text = parts[1]
        if hours < 0 or minutes < 0 or minutes >= 60:
            raise ValueError
        if "." in seconds_text and "," in seconds_text:
            raise ValueError
        timestamp_parts = re.split(r"[.,]", seconds_text, maxsplit=1)
        whole = timestamp_parts[0]
        fraction = timestamp_parts[1] if len(timestamp_parts) == 2 else ""
        seconds = int(whole)
        if seconds < 0 or seconds >= 60:
            raise ValueError
        if fraction and (not fraction.isdigit() or len(fraction) > 3):
            raise ValueError
        millis = int((fraction + "000")[:3]) if fraction else 0
    except (TypeError, ValueError) as error:
        raise SubtitleParseError("字幕时间戳格式无效") from error
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _safe_text(lines: list[str]) -> str:
    raw = " ".join(line.strip() for line in lines if line.strip())
    if not raw:
        raise SubtitleParseError("字幕 Cue 文本为空")
    if "\x00" in raw or any(
        ord(char) < 32 and char not in {"\t"} for char in raw
    ):
        raise SubtitleParseError("字幕文本包含控制字符")
    # Decode entities before checking markup.  Otherwise ``&lt;script&gt;``
    # would become executable-looking content only after the safety check.
    decoded = unescape(raw)
    if _SUSPICIOUS.search(decoded):
        raise SubtitleParseError("字幕文本包含不安全内容")
    # Formatting tags are not knowledge content.  Remove only the small
    # WebVTT/SRT-compatible subset and reject all remaining markup.
    cleaned = _ALLOWED_TAG.sub("", decoded)
    if "<" in cleaned or ">" in cleaned:
        raise SubtitleParseError("字幕文本包含不支持的标记")
    expanded = unescape(cleaned)
    if "<" in expanded or ">" in expanded or _SUSPICIOUS.search(expanded):
        raise SubtitleParseError("字幕文本包含不安全内容")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise SubtitleParseError("字幕 Cue 文本为空")
    if len(cleaned) > 100_000:
        raise SubtitleParseError("字幕 Cue 文本超过大小限制")
    return cleaned


def _parse_timed_cues(text: str, fmt: str) -> list[tuple[int, int, str]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if fmt == "vtt":
        if not lines or lines[0].strip().upper() != "WEBVTT":
            raise SubtitleParseError("WebVTT 头无效")
        lines = lines[1:]
    cues: list[tuple[int, int, str]] = []
    block: list[str] = []

    def consume(current: list[str]) -> None:
        if not current:
            return
        if fmt == "vtt" and current[0].strip().upper().startswith(("NOTE", "STYLE", "REGION")):
            return
        timing_index = next(
            (index for index, line in enumerate(current) if "-->" in line), None
        )
        if timing_index is None:
            raise SubtitleParseError("字幕 Cue 缺少时间轴")
        match = _TIMING.fullmatch(current[timing_index].strip())
        if match is None:
            raise SubtitleParseError("字幕时间轴格式无效")
        start = parse_timestamp(match.group("start"))
        end = parse_timestamp(match.group("end"))
        if end <= start:
            raise SubtitleParseError("字幕时间段必须为非空区间")
        cue_text = _safe_text(current[timing_index + 1 :])
        cues.append((start, end, cue_text))

    for line in lines:
        if line.strip():
            block.append(line)
        else:
            consume(block)
            block = []
    consume(block)
    if not cues:
        raise SubtitleParseError("字幕没有有效 Cue")
    return cues


def _strict_int(value: Any, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SubtitleParseError(message)
    return value


def _parse_json3(text: str) -> list[tuple[int, int, str]]:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise SubtitleParseError("JSON 字幕格式无效") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise SubtitleParseError("JSON 字幕缺少 events")
    cues: list[tuple[int, int, str]] = []
    for event in payload["events"]:
        if not isinstance(event, dict):
            raise SubtitleParseError("JSON 字幕事件格式无效")
        start = _strict_int(event.get("tStartMs"), "JSON 字幕起点无效")
        duration = _strict_int(event.get("dDurationMs"), "JSON 字幕时长无效")
        if start < 0 or duration <= 0:
            raise SubtitleParseError("JSON 字幕时间段无效")
        segments = event.get("segs")
        if not isinstance(segments, list):
            continue
        text_parts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("utf8"), str):
                raise SubtitleParseError("JSON 字幕文本格式无效")
            text_parts.append(segment["utf8"])
        if text_parts:
            cues.append((start, start + duration, _safe_text(text_parts)))
    if not cues:
        raise SubtitleParseError("JSON 字幕没有有效 Cue")
    return cues


def _detect_format(track: SubtitleTrack, text: str) -> str:
    if track.format != "unknown":
        return track.format
    stripped = text.lstrip()
    if stripped.upper().startswith("WEBVTT"):
        return "vtt"
    if stripped.startswith("{"):
        return "json3"
    if "-->" in stripped:
        return "srt"
    raise SubtitleParseError("无法识别字幕格式")


def normalize_subtitle_track(
    track: SubtitleTrack,
    *,
    duration_ms: int | None = None,
    max_bytes: int = 10_000_000,
    max_segments: int = 50_000,
) -> list[TranscriptSegment]:
    if len(track.content) > max_bytes:
        raise SubtitleParseError("字幕文件超过大小限制")
    try:
        text = track.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SubtitleParseError("字幕必须使用 UTF-8 编码") from error
    fmt = _detect_format(track, text)
    if fmt == "json3":
        cues = _parse_json3(text)
    elif fmt in {"vtt", "srt"}:
        cues = _parse_timed_cues(text, fmt)
    else:
        raise SubtitleParseError("字幕格式不受支持")
    if len(cues) > max_segments:
        raise SubtitleParseError("字幕 Cue 数量超过限制")

    language = track.language.strip() if isinstance(track.language, str) else None
    if language == "":
        language = None
    normalized: list[TranscriptSegment] = []
    previous_start = -1
    for index, (start, end, cue_text) in enumerate(cues):
        if start < previous_start:
            raise SubtitleParseError(f"字幕 Cue 顺序无效：第 {index + 1} 段乱序")
        if duration_ms is not None and end > duration_ms:
            raise SubtitleParseError("字幕时间超出视频时长")
        normalized.append(
            TranscriptSegment(
                start_ms=start,
                end_ms=end,
                duration_ms=duration_ms,
                text=cue_text,
                language=language,
            )
        )
        previous_start = start
    return normalized


def format_timestamp(milliseconds: int) -> str:
    if milliseconds < 0:
        raise ValueError("timestamp must be non-negative")
    seconds, millis = divmod(milliseconds, 1000)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}.{millis:03d}"


def render_transcript_vtt(segments: list[TranscriptSegment]) -> bytes:
    lines = ["WEBVTT", ""]
    for segment in segments:
        lines.extend(
            [
                f"{format_timestamp(segment.start_ms)} --> {format_timestamp(segment.end_ms)}",
                segment.text,
                "",
            ]
        )
    return "\n".join(lines).encode("utf-8")


__all__ = [
    "SubtitleParseError",
    "format_timestamp",
    "normalize_subtitle_track",
    "parse_timestamp",
    "render_transcript_vtt",
]
