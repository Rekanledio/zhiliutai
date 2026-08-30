"""Bounded, non-sensitive tag names used by review and Markdown Frontmatter."""

from __future__ import annotations

import re

from app.core.safety import redact_sensitive_text


MAX_TAGS_PER_ITEM = 50
MAX_TAG_TEXT_CHARS = 80

_TRACEBACK_MARKER = re.compile(r"(?i)\b(?:traceback|stack\s+trace)\b")
_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\)")


def normalize_tag_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("标签必须是字符串")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_TAG_TEXT_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        or normalized.startswith("/")
        or _ABSOLUTE_WINDOWS_PATH.search(normalized)
        or _TRACEBACK_MARKER.search(normalized)
        or redact_sensitive_text(normalized) != normalized
    ):
        raise ValueError("标签内容无效")
    return normalized


def normalize_tag_names(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_TAGS_PER_ITEM:
        raise ValueError("标签列表无效")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = normalize_tag_text(item)
        folded = normalized.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(normalized)
    return result
