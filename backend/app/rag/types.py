from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk rehydrated from SQLite after retrieval-channel validation."""

    chunk_id: str
    knowledge_item_id: str
    content_version_id: str
    item_title: str
    version_no: int
    source_type: str
    content: str
    source_locator: str
    ordinal: int = 0
    content_hash: str = ""
    matched_by: tuple[str, ...] = ()
    fts_rank: int | None = None
    vector_rank: int | None = None
    fts_score: float | None = None
    vector_score: float | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "knowledge_item_id": self.knowledge_item_id,
            "content_version_id": self.content_version_id,
            "item_title": self.item_title,
            "version_no": self.version_no,
            "source_type": self.source_type,
            "content": self.content,
            "source_locator": self.source_locator,
            "matched_by": list(self.matched_by),
            "fts_rank": self.fts_rank,
            "vector_rank": self.vector_rank,
            "fts_score": self.fts_score,
            "vector_score": self.vector_score,
            "rrf_score": self.rrf_score,
            "rerank_score": self.rerank_score,
        }


@dataclass(frozen=True)
class RetrievalDiagnostics:
    original_query: str
    normalized_query: str
    fts_query: str | None
    fts_available: bool
    vector_available: bool
    degraded: bool = False
    channel_errors: dict[str, str] = field(default_factory=dict)
    reranker_available: bool = False

    def as_dict(self) -> dict[str, Any]:
        from app.core.safety import redact_sensitive_text

        return {
            "original_query": redact_sensitive_text(self.original_query),
            "normalized_query": redact_sensitive_text(self.normalized_query),
            "fts_query": redact_sensitive_text(self.fts_query) if self.fts_query else None,
            "fts_available": self.fts_available,
            "vector_available": self.vector_available,
            "degraded": self.degraded,
            "channel_errors": {
                redact_sensitive_text(str(key)): redact_sensitive_text(str(value))
                for key, value in self.channel_errors.items()
            },
            "reranker_available": self.reranker_available,
        }
