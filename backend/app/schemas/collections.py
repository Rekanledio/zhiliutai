from __future__ import annotations

import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.core.safety import redact_sensitive_text
from app.workflows.contracts import CanonicalId


MAX_COLLECTIONS_PER_ITEM = 50
MAX_COLLECTION_TEXT_CHARS = 200
MAX_COLLECTION_DESCRIPTION_CHARS = 2_000

_TRACEBACK_MARKER = re.compile(r"(?i)\b(?:traceback|stack\s+trace)\b")
_ABSOLUTE_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\)")
_ABSOLUTE_UNIX_PATH = re.compile(r"(?<![A-Za-z0-9])/((?!/)[^\s/]+/)+[^\s/]*")


def normalize_collection_text(
    value: object,
    *,
    max_length: int,
    allow_empty: bool = False,
) -> str | None:
    """Validate user-visible collection text without retaining unsafe input."""

    if value is None and allow_empty:
        return None
    if not isinstance(value, str):
        raise ValueError("合集文本必须是字符串")
    normalized = value.strip()
    if not normalized and allow_empty:
        return None
    if not normalized or len(normalized) > max_length:
        raise ValueError("合集文本长度无效")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("合集文本包含不支持的字符")
    if (
        _TRACEBACK_MARKER.search(normalized)
        or _ABSOLUTE_WINDOWS_PATH.search(normalized)
        or _ABSOLUTE_UNIX_PATH.search(normalized)
        or redact_sensitive_text(normalized) != normalized
    ):
        raise ValueError("合集文本包含不允许的敏感内容")
    return normalized


def normalize_collection_names(value: object) -> list[str]:
    """Validate the bounded string list used by Markdown collections."""

    if not isinstance(value, list) or len(value) > MAX_COLLECTIONS_PER_ITEM:
        raise ValueError("合集列表无效")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = normalize_collection_text(
            item,
            max_length=MAX_COLLECTION_TEXT_CHARS,
        )
        assert normalized is not None
        folded = normalized.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(normalized)
    return result


class CollectionWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: StrictStr = Field(min_length=1, max_length=MAX_COLLECTION_TEXT_CHARS)
    description: StrictStr | None = Field(
        default=None,
        max_length=MAX_COLLECTION_DESCRIPTION_CHARS,
    )

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str:
        normalized = normalize_collection_text(
            value,
            max_length=MAX_COLLECTION_TEXT_CHARS,
        )
        assert normalized is not None
        return normalized

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, value: object) -> str | None:
        return normalize_collection_text(
            value,
            max_length=MAX_COLLECTION_DESCRIPTION_CHARS,
            allow_empty=True,
        )


class CollectionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: StrictStr | None = Field(default=None, max_length=MAX_COLLECTION_TEXT_CHARS)
    description: StrictStr | None = Field(
        default=None,
        max_length=MAX_COLLECTION_DESCRIPTION_CHARS,
    )

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: object) -> str | None:
        if value is None:
            return None
        return normalize_collection_text(value, max_length=MAX_COLLECTION_TEXT_CHARS)

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, value: object) -> str | None:
        return normalize_collection_text(
            value,
            max_length=MAX_COLLECTION_DESCRIPTION_CHARS,
            allow_empty=True,
        )

    @model_validator(mode="after")
    def _require_update(self) -> CollectionUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("合集更新内容不能为空")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("合集名称无效")
        return self


class CollectionItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: CanonicalId
    title: StrictStr
    source_type: StrictStr
    version_no: StrictInt = Field(ge=1)
    suggested_tags: list[StrictStr] = Field(default_factory=list, max_length=50)


class CollectionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: CanonicalId
    name: StrictStr
    description: StrictStr | None = None
    item_count: StrictInt = Field(ge=0)
    moc_enabled: StrictBool = False


class CollectionResponse(CollectionSummaryResponse):
    items: list[CollectionItemResponse] = Field(default_factory=list, max_length=1000)
    related_tags: list[StrictStr] = Field(default_factory=list, max_length=50)
    moc_status: Literal["not_enabled"] = "not_enabled"
