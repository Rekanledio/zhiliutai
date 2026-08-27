from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import NoteBinding, SourceArtifact
from app.rag.types import RetrievedChunk


@dataclass(frozen=True)
class BuiltCitation:
    citation_id: str
    chunk_id: str
    knowledge_item_id: str
    content_version_id: str
    item_title: str
    version_no: int
    source_type: str
    excerpt: str
    chunk_content_hash: str
    locator_status: str
    locator: dict[str, Any]
    target: dict[str, Any]
    retrieval: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "chunk_id": self.chunk_id,
            "knowledge_item_id": self.knowledge_item_id,
            "content_version_id": self.content_version_id,
            "item_title": self.item_title,
            "version_no": self.version_no,
            "source_type": self.source_type,
            "excerpt": self.excerpt,
            "chunk_content_hash": self.chunk_content_hash,
            "locator_status": self.locator_status,
            "locator": dict(self.locator),
            "target": dict(self.target),
            "retrieval": dict(self.retrieval),
        }


def _safe_relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix() if path.as_posix() == value else None


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    sensitive_query_keys = {
        "api_key",
        "apikey",
        "access_token",
        "auth",
        "authorization",
        "password",
        "secret",
        "token",
    }
    if any(
        key.casefold().replace("-", "_") in sensitive_query_keys
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _heading_path(value: object) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(part, str) or not part for part in value):
        return None
    return value[:12]


def _excerpt(value: str, limit: int = 600) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[:limit].rstrip() + "…"


class CitationBuilder:
    """Builds citations only from authoritative SQLite chunks and metadata."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def _metadata(
        self, item_ids: Sequence[str]
    ) -> tuple[dict[str, list[SourceArtifact]], dict[str, NoteBinding]]:
        if not item_ids:
            return {}, {}
        async with self.session_factory() as session:
            artifacts = list(
                (
                    await session.execute(
                        select(SourceArtifact).where(
                            SourceArtifact.knowledge_item_id.in_(item_ids)
                        )
                    )
                ).scalars()
            )
            bindings = list(
                (
                    await session.execute(
                        select(NoteBinding).where(NoteBinding.knowledge_item_id.in_(item_ids))
                    )
                ).scalars()
            )
        artifact_map: dict[str, list[SourceArtifact]] = {}
        for artifact in artifacts:
            artifact_map.setdefault(artifact.knowledge_item_id, []).append(artifact)
        return artifact_map, {binding.knowledge_item_id: binding for binding in bindings}

    @staticmethod
    def _fallback(
        chunk: RetrievedChunk,
        binding: NoteBinding | None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        path = _safe_relative_path(binding.relative_path) if binding else None
        if path is None:
            return "unavailable", {"kind": "none"}, {"kind": "none"}
        return (
            "fallback",
            {"kind": "obsidian", "path": path},
            {"kind": "obsidian", "item_id": chunk.knowledge_item_id},
        )

    @classmethod
    def _locate(
        cls,
        chunk: RetrievedChunk,
        artifacts: Sequence[SourceArtifact],
        binding: NoteBinding | None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        try:
            locator = json.loads(chunk.source_locator)
        except (TypeError, json.JSONDecodeError):
            locator = None
        if not isinstance(locator, dict):
            return cls._fallback(chunk, binding)
        kind = locator.get("kind")

        if kind == "pdf" and chunk.source_type == "pdf":
            page = _positive_int(locator.get("page"))
            page_label = locator.get("page_label")
            page_label = page_label if isinstance(page_label, str) and page_label else None
            artifact = next(
                (
                    artifact
                    for artifact in artifacts
                    if artifact.artifact_type == "original_input"
                    and artifact.media_type == "application/pdf"
                ),
                None,
            )
            if page is not None and artifact is not None:
                return (
                    "exact",
                    {"kind": "pdf", "page": page, "page_label": page_label},
                    {"kind": "artifact", "artifact_id": artifact.id, "page": page},
                )
            return cls._fallback(chunk, binding)

        if kind == "docx" and chunk.source_type == "docx":
            element = locator.get("element")
            heading_path = _heading_path(locator.get("heading_path"))
            valid = heading_path is not None
            if element == "heading":
                valid = valid and _positive_int(locator.get("heading_level")) in range(1, 7)
            elif element == "paragraph":
                valid = valid and _positive_int(locator.get("paragraph")) is not None
            elif element == "table_row":
                valid = (
                    valid
                    and _positive_int(locator.get("table")) is not None
                    and _positive_int(locator.get("row")) is not None
                )
            else:
                valid = False
            artifact = next(
                (
                    artifact
                    for artifact in artifacts
                    if artifact.artifact_type == "original_input"
                    and artifact.media_type
                    == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
                None,
            )
            if valid and artifact is not None:
                normalized = {
                    "kind": "docx",
                    "element": element,
                    "heading_path": heading_path,
                }
                for key in ("heading_level", "paragraph", "table", "row"):
                    if key in locator:
                        normalized[key] = locator[key]
                return "exact", normalized, {"kind": "artifact", "artifact_id": artifact.id}
            return cls._fallback(chunk, binding)

        if kind == "webpage" and chunk.source_type == "webpage":
            url = _safe_url(locator.get("url"))
            if url is not None:
                normalized = {
                    "kind": "webpage",
                    "url": url,
                    "element": locator.get("element")
                    if isinstance(locator.get("element"), str)
                    else None,
                }
                heading_path = _heading_path(locator.get("heading_path"))
                if heading_path is not None:
                    normalized["heading_path"] = heading_path
                return "exact", normalized, {"kind": "url", "url": url}
            return cls._fallback(chunk, binding)

        if kind == "obsidian" and chunk.source_type in {"text", "markdown", "webpage", "pdf", "docx"}:
            path = _safe_relative_path(locator.get("path"))
            binding_path = _safe_relative_path(binding.relative_path) if binding else None
            if path is not None and binding_path == path:
                return (
                    "exact",
                    {"kind": "obsidian", "path": path},
                    {"kind": "obsidian", "item_id": chunk.knowledge_item_id},
                )
            return cls._fallback(chunk, binding)

        return cls._fallback(chunk, binding)

    async def build(self, chunks: Sequence[RetrievedChunk]) -> list[BuiltCitation]:
        artifacts, bindings = await self._metadata(
            list(dict.fromkeys(chunk.knowledge_item_id for chunk in chunks))
        )
        citations: list[BuiltCitation] = []
        for index, chunk in enumerate(chunks, start=1):
            status, locator, target = self._locate(
                chunk,
                artifacts.get(chunk.knowledge_item_id, []),
                bindings.get(chunk.knowledge_item_id),
            )
            citations.append(
                BuiltCitation(
                    citation_id=f"C{index}",
                    chunk_id=chunk.chunk_id,
                    knowledge_item_id=chunk.knowledge_item_id,
                    content_version_id=chunk.content_version_id,
                    item_title=chunk.item_title,
                    version_no=chunk.version_no,
                    source_type=chunk.source_type,
                    excerpt=_excerpt(chunk.content),
                    chunk_content_hash=chunk.content_hash,
                    locator_status=status,
                    locator=locator,
                    target=target,
                    retrieval={
                        "matched_by": list(chunk.matched_by),
                        "fts_rank": chunk.fts_rank,
                        "vector_rank": chunk.vector_rank,
                        "fts_score": chunk.fts_score,
                        "vector_score": chunk.vector_score,
                        "rrf_score": chunk.rrf_score,
                        "rerank_score": chunk.rerank_score,
                    },
                )
            )
        return citations
