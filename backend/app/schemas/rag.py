from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SourceType = Literal["text", "markdown", "pdf", "docx", "webpage"]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=6, ge=1, le=20)
    rewrite: Literal["auto", "off"] = "off"
    source_types: list[SourceType] | None = Field(default=None, max_length=5)

    model_config = ConfigDict(extra="forbid")


class ChatRequest(SearchRequest):
    pass


class CitationLocator(BaseModel):
    kind: Literal["pdf", "docx", "webpage", "obsidian", "none"]
    page: int | None = Field(default=None, ge=1)
    page_label: str | None = None
    element: str | None = None
    heading_level: int | None = Field(default=None, ge=1, le=6)
    heading_path: list[str] | None = None
    paragraph: int | None = Field(default=None, ge=1)
    table: int | None = Field(default=None, ge=1)
    row: int | None = Field(default=None, ge=1)
    url: str | None = None
    path: str | None = None

    model_config = ConfigDict(extra="forbid")


class CitationTarget(BaseModel):
    kind: Literal["artifact", "url", "obsidian", "none"]
    artifact_id: str | None = None
    item_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    url: str | None = None

    model_config = ConfigDict(extra="forbid")


class RetrievalInfo(BaseModel):
    matched_by: list[str] = Field(default_factory=list)
    fts_rank: int | None = None
    vector_rank: int | None = None
    fts_score: float | None = None
    vector_score: float | None = None
    rrf_score: float
    rerank_score: float | None = None

    model_config = ConfigDict(extra="forbid")


class CitationResponse(BaseModel):
    citation_id: str
    chunk_id: str
    knowledge_item_id: str
    content_version_id: str
    item_title: str
    version_no: int
    source_type: SourceType | str
    excerpt: str
    chunk_content_hash: str
    locator_status: Literal["exact", "fallback", "unavailable"]
    locator: CitationLocator
    target: CitationTarget
    retrieval: RetrievalInfo

    model_config = ConfigDict(extra="forbid")


class SearchResult(BaseModel):
    chunk_id: str
    knowledge_item_id: str
    content_version_id: str
    item_title: str
    version_no: int
    source_type: SourceType | str
    excerpt: str
    citation: CitationResponse

    model_config = ConfigDict(extra="forbid")


class EvidenceResponse(BaseModel):
    status: Literal["none", "low_confidence", "sufficient"]
    reason: str

    model_config = ConfigDict(extra="forbid")


class RetrievalDiagnosticsResponse(BaseModel):
    original_query: str
    normalized_query: str
    fts_query: str | None
    fts_available: bool
    vector_available: bool
    degraded: bool
    channel_errors: dict[str, str] = Field(default_factory=dict)
    reranker_available: bool = False

    model_config = ConfigDict(extra="forbid")


class SearchResponse(BaseModel):
    query: str
    normalized_query: str
    results: list[SearchResult]
    evidence: EvidenceResponse
    diagnostics: RetrievalDiagnosticsResponse
    searched_at: datetime

    model_config = ConfigDict(extra="forbid")
