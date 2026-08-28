"""Strict input contracts for the five provider-side MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, StrictInt, StrictStr
from pydantic.main import BaseModel

from app.workflows.contracts import CanonicalId


SourceType = Literal["text", "markdown"]
SearchSourceType = Literal["text", "markdown", "pdf", "docx", "webpage", "video"]


class _McpInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AddTextInput(_McpInput):
    content: StrictStr = Field(min_length=1, max_length=1_000_000)
    source_type: SourceType = "text"
    title: StrictStr | None = Field(default=None, max_length=300)
    idempotency_key: StrictStr | None = Field(default=None, min_length=1, max_length=200)


class AddUrlInput(_McpInput):
    url: StrictStr = Field(min_length=1, max_length=2_048)
    title: StrictStr | None = Field(default=None, max_length=300)
    idempotency_key: StrictStr | None = Field(default=None, min_length=1, max_length=200)


class SearchKnowledgeInput(_McpInput):
    query: StrictStr = Field(min_length=1, max_length=2_000)
    limit: StrictInt = Field(default=6, ge=1, le=20)
    source_types: list[SearchSourceType] | None = Field(default=None, max_length=6)


class GetItemInput(_McpInput):
    item_id: CanonicalId


class ListCollectionsInput(_McpInput):
    limit: StrictInt = Field(default=100, ge=1, le=100)
