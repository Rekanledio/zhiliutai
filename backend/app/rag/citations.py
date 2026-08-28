from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.paths import safe_relative_path
from app.core.safety import redact_sensitive_text, redact_sensitive_value
from app.db.models import Chunk, ContentVersion, KnowledgeItem, NoteBinding, SourceArtifact
from app.obsidian.markdown import ObsidianVault
from app.rag.types import RetrievedChunk
from app.services.artifacts import ArtifactStore
from app.services.content import chunk_content, content_hash


PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WEB_MEDIA_TYPES = {"text/html", "application/xhtml+xml"}
VIDEO_TRANSCRIPT_MEDIA_TYPES = {"text/vtt", "text/plain"}
VIDEO_KEYFRAME_MEDIA_TYPES = {"image/webp", "image/png", "image/jpeg"}


class CitationBuildError(RuntimeError):
    """Raised when a retrieved chunk is no longer an authoritative citation."""


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
            "item_title": redact_sensitive_text(self.item_title),
            "version_no": self.version_no,
            "source_type": self.source_type,
            "excerpt": self.excerpt,
            "chunk_content_hash": self.chunk_content_hash,
            "locator_status": self.locator_status,
            "locator": redact_sensitive_value(self.locator),
            "target": redact_sensitive_value(self.target),
            "retrieval": redact_sensitive_value(self.retrieval),
        }


@dataclass(frozen=True)
class _AuthoritativeCitationData:
    chunk: Chunk
    version: ContentVersion
    item: KnowledgeItem
    artifacts: tuple[SourceArtifact, ...]
    binding: NoteBinding | None


def _safe_relative_path(value: object) -> str | None:
    """Compatibility wrapper kept for callers of the former local helper."""

    return safe_relative_path(value)


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 4096 or value != value.strip():
        return None
    from urllib.parse import parse_qsl, urlsplit

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    sensitive_query_keys = {
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
        key.casefold().replace("-", "_") in sensitive_query_keys
        for raw_query in (parsed.query, parsed.fragment)
        for key, _ in parse_qsl(raw_query, keep_blank_values=True)
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


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _heading_path(value: object, *, allow_empty: bool = False) -> list[str] | None:
    if not isinstance(value, list) or len(value) > 12:
        return None
    if not allow_empty and not value:
        return None
    if any(
        not isinstance(part, str) or not part or part != part.strip() for part in value
    ):
        return None
    return list(value)


def _excerpt(value: str, limit: int = 600) -> str:
    compact = " ".join(redact_sensitive_text(value).split())
    return compact if len(compact) <= limit else compact[:limit].rstrip() + "…"


def _json_object(value: str | None) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


class CitationBuilder:
    """Build citations from current SQLite rows and verified source metadata."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        artifact_store: ArtifactStore | None = None,
        vault: ObsidianVault | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.artifact_store = artifact_store
        self.vault = vault

    async def _authoritative_data(
        self, chunks: Sequence[RetrievedChunk]
    ) -> dict[str, _AuthoritativeCitationData]:
        chunk_ids = list(dict.fromkeys(chunk.chunk_id for chunk in chunks))
        if not chunk_ids:
            return {}
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(Chunk, ContentVersion, KnowledgeItem)
                    .join(ContentVersion, ContentVersion.id == Chunk.content_version_id)
                    .join(KnowledgeItem, KnowledgeItem.id == Chunk.knowledge_item_id)
                    .where(
                        Chunk.id.in_(chunk_ids),
                        Chunk.knowledge_item_id == KnowledgeItem.id,
                        ContentVersion.knowledge_item_id == KnowledgeItem.id,
                        KnowledgeItem.status == "published",
                        KnowledgeItem.deleted_at.is_(None),
                        KnowledgeItem.current_content_version_id == ContentVersion.id,
                        KnowledgeItem.current_content_version_id == Chunk.content_version_id,
                    )
                )
            ).all()
            authoritative = {
                chunk.id: (chunk, version, item) for chunk, version, item in rows
            }
            item_ids = list(dict.fromkeys(item.id for _, _, item in rows))
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
        binding_map = {binding.knowledge_item_id: binding for binding in bindings}
        return {
            chunk_id: _AuthoritativeCitationData(
                chunk=chunk,
                version=version,
                item=item,
                artifacts=tuple(artifact_map.get(item.id, ())),
                binding=binding_map.get(item.id),
            )
            for chunk_id, (chunk, version, item) in authoritative.items()
        }

    def _artifact_is_valid(self, artifact: SourceArtifact) -> bool:
        if safe_relative_path(artifact.relative_path) is None:
            return False
        if self.artifact_store is None:
            return False
        return self.artifact_store.verify(
            artifact.relative_path,
            artifact.content_hash,
            artifact.byte_size,
        )

    def _artifact(
        self,
        metadata: Mapping[str, Any],
        artifacts: Sequence[SourceArtifact],
        *,
        key: str,
        artifact_type: str,
        media_types: set[str],
    ) -> SourceArtifact | None:
        artifact_id = metadata.get(key)
        if not isinstance(artifact_id, str) or not artifact_id:
            return None
        artifact = next(
            (
                candidate
                for candidate in artifacts
                if candidate.id == artifact_id
                and candidate.artifact_type == artifact_type
                and candidate.media_type in media_types
            ),
            None,
        )
        return artifact if artifact is not None and self._artifact_is_valid(artifact) else None

    def _artifact_by_id_or_hash(
        self,
        value: object,
        artifacts: Sequence[SourceArtifact],
        *,
        artifact_type: str,
        media_types: set[str],
    ) -> SourceArtifact | None:
        if not isinstance(value, str) or not value:
            return None
        artifact = next(
            (
                candidate
                for candidate in artifacts
                if candidate.artifact_type == artifact_type
                and candidate.media_type in media_types
                and (candidate.id == value or candidate.content_hash == value)
            ),
            None,
        )
        return artifact if artifact is not None and self._artifact_is_valid(artifact) else None

    @staticmethod
    def _video_span(
        locator: Mapping[str, Any], duration_ms: int | None
    ) -> tuple[int, int] | None:
        start_ms = _non_negative_int(locator.get("start_ms"))
        end_ms = _positive_int(locator.get("end_ms"))
        if start_ms is None or end_ms is None or start_ms >= end_ms:
            return None
        if duration_ms is not None and end_ms > duration_ms:
            return None
        return start_ms, end_ms

    def _video_artifacts(
        self,
        data: _AuthoritativeCitationData,
        metadata: Mapping[str, Any],
    ) -> tuple[SourceArtifact, SourceArtifact | None, dict[str, Any], int | None] | None:
        source = self._artifact(
            metadata,
            data.artifacts,
            key="source_artifact_id",
            artifact_type="video_source",
            media_types={"text/uri-list"},
        )
        if source is None:
            return None
        source_locator = _json_object(source.source_locator)
        if source_locator is None or source_locator.get("kind") != "video_url":
            return None
        requested_url = _safe_url(source_locator.get("requested_url"))
        metadata_requested = _safe_url(metadata.get("requested_url") or metadata.get("source_url"))
        final_url = _safe_url(metadata.get("final_url") or metadata.get("url"))
        if requested_url is None or metadata_requested != requested_url or final_url is None:
            return None
        video_metadata = metadata.get("video")
        if not isinstance(video_metadata, dict):
            video_metadata = {}
        duration_ms = _non_negative_int(video_metadata.get("duration_ms"))
        if duration_ms is None:
            manifest = metadata.get("manifest")
            if isinstance(manifest, dict):
                manifest_source = manifest.get("source_metadata")
                if isinstance(manifest_source, dict):
                    duration_ms = _non_negative_int(manifest_source.get("duration_ms"))
        transcript = self._artifact_by_id_or_hash(
            metadata.get("transcript_artifact_id"),
            data.artifacts,
            artifact_type="video_transcript",
            media_types=VIDEO_TRANSCRIPT_MEDIA_TYPES,
        )
        return source, transcript, {"requested_url": requested_url, "final_url": final_url}, duration_ms

    def _binding_path(
        self,
        item: KnowledgeItem,
        binding: NoteBinding | None,
        *,
        require_synced: bool = False,
        expected_content_hash: str | None = None,
    ) -> str | None:
        if binding is None:
            return None
        if self.vault is None:
            return None
        if binding.knowledge_item_id != item.id or binding.zhiliu_id != item.id:
            return None
        if binding.sync_state in {"missing", "conflict", "error"}:
            return None
        if require_synced and binding.sync_state != "synced":
            return None
        if expected_content_hash is not None and binding.content_hash != expected_content_hash:
            return None
        path = safe_relative_path(binding.relative_path)
        if path is None or not path.casefold().endswith(".md"):
            return None
        try:
            if not self.vault.resolve(path).is_file():
                return None
            if self.vault.hash(path) != binding.content_hash:
                return None
        except (OSError, ValueError):
            return None
        return path

    def _locate_video(
        self,
        data: _AuthoritativeCitationData,
        metadata: Mapping[str, Any],
        locator: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        video_data = self._video_artifacts(data, metadata)
        if video_data is None:
            return None
        _source, transcript, _urls, duration_ms = video_data
        if transcript is None:
            return None
        kind = locator.get("kind")
        span = self._video_span(locator, duration_ms)
        if span is None:
            return None
        start_ms, end_ms = span
        manifest = metadata.get("manifest")
        if not isinstance(manifest, dict):
            return None

        if kind == "video":
            language = locator.get("language")
            if language is not None and (
                not isinstance(language, str)
                or not language
                or language != language.strip()
                or len(language) > 32
            ):
                return None
            raw_segments = manifest.get("transcript_segments")
            if not isinstance(raw_segments, list):
                return None
            matching_segment = next(
                (
                    segment
                    for segment in raw_segments
                    if isinstance(segment, dict)
                    and segment.get("start_ms") == start_ms
                    and segment.get("end_ms") == end_ms
                    and (language is None or segment.get("language") == language)
                    and isinstance(segment.get("text"), str)
                    and segment["text"] in data.chunk.content
                ),
                None,
            )
            if matching_segment is None:
                return None
            normalized: dict[str, Any] = {
                "kind": "video",
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
            if language is not None:
                normalized["language"] = language
            return (
                "exact",
                normalized,
                {
                    "kind": "artifact",
                    "artifact_id": transcript.id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                },
            )

        if kind == "video_chapter":
            raw_chapters = manifest.get("chapters")
            if not isinstance(raw_chapters, list) or not any(
                isinstance(chapter, dict)
                and chapter.get("start_ms") == start_ms
                and chapter.get("end_ms") == end_ms
                and isinstance(chapter.get("title"), str)
                and chapter["title"] in data.chunk.content
                for chapter in raw_chapters
            ):
                return None
            return (
                "exact",
                {"kind": "video_chapter", "start_ms": start_ms, "end_ms": end_ms},
                {
                    "kind": "artifact",
                    "artifact_id": transcript.id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                },
            )

        if kind != "video_keyframe":
            return None
        raw_keyframe_ids = locator.get("keyframe_ids")
        if (
            not isinstance(raw_keyframe_ids, list)
            or not raw_keyframe_ids
            or len(raw_keyframe_ids) > 64
            or any(
                not isinstance(keyframe_id, str)
                or not keyframe_id
                or len(keyframe_id) > 200
                for keyframe_id in raw_keyframe_ids
            )
            or len(set(raw_keyframe_ids)) != len(raw_keyframe_ids)
        ):
            return None
        raw_events = manifest.get("visual_events")
        if not isinstance(raw_events, list):
            return None
        event_type = locator.get("event_type")
        if event_type is not None and (
            not isinstance(event_type, str) or event_type not in {"scene", "slide", "code", "ui", "speaker", "other"}
        ):
            return None
        matching_event = next(
            (
                event
                for event in raw_events
                if isinstance(event, dict)
                and event.get("start_ms") == start_ms
                and event.get("end_ms") == end_ms
                and event.get("keyframe_ids") == raw_keyframe_ids
                and (event_type is None or event.get("event_type") == event_type)
                and isinstance(event.get("summary"), str)
                and event["summary"] in data.chunk.content
            ),
            None,
        )
        if matching_event is None:
            return None
        raw_keyframes = manifest.get("keyframes")
        if not isinstance(raw_keyframes, list):
            return None
        keyframes = {
            keyframe.get("keyframe_id"): keyframe
            for keyframe in raw_keyframes
            if isinstance(keyframe, dict) and isinstance(keyframe.get("keyframe_id"), str)
        }
        selected_artifact: SourceArtifact | None = None
        for keyframe_id in raw_keyframe_ids:
            keyframe = keyframes.get(keyframe_id)
            if keyframe is None:
                return None
            keyframe_artifact = self._artifact_by_id_or_hash(
                keyframe.get("artifact_id"),
                data.artifacts,
                artifact_type="video_keyframe",
                media_types=VIDEO_KEYFRAME_MEDIA_TYPES,
            )
            if keyframe_artifact is None:
                return None
            content_hash_value = keyframe.get("content_hash")
            if content_hash_value is not None and content_hash_value != keyframe_artifact.content_hash:
                return None
            if selected_artifact is None:
                selected_artifact = keyframe_artifact
        if selected_artifact is None:
            return None
        normalized = {
            "kind": "video_keyframe",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "keyframe_ids": list(raw_keyframe_ids),
        }
        if event_type is not None:
            normalized["event_type"] = event_type
        return (
            "exact",
            normalized,
            {
                "kind": "artifact",
                "artifact_id": selected_artifact.id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "keyframe_id": raw_keyframe_ids[0],
            },
        )

    def _fallback(
        self,
        data: _AuthoritativeCitationData,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        path = self._binding_path(
            data.item,
            data.binding,
            require_synced=True,
            expected_content_hash=data.version.content_hash,
        )
        if path is None:
            return "unavailable", {"kind": "none"}, {"kind": "none"}
        return (
            "fallback",
            {"kind": "obsidian", "path": path},
            {"kind": "obsidian", "item_id": data.item.id},
        )

    @staticmethod
    def _metadata_parts(
        version: ContentVersion,
    ) -> tuple[dict[str, Any], list[tuple[str, object]]] | None:
        metadata = _json_object(version.source_metadata_json)
        if metadata is None:
            return None
        segments = metadata.get("segments")
        if not isinstance(segments, list) or not segments:
            return None
        parts: list[tuple[str, object]] = []
        texts: list[str] = []
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                return None
            text = segment["text"]
            try:
                segment_parts = chunk_content(text)
            except ValueError:
                return None
            if not segment_parts:
                return None
            texts.append(text)
            parts.extend((part, segment.get("locator")) for part in segment_parts)
        try:
            if content_hash("\n\n".join(texts)) != content_hash(version.body):
                return None
        except ValueError:
            return None
        return metadata, parts

    @classmethod
    def _metadata_locator(
        cls,
        data: _AuthoritativeCitationData,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        metadata_parts = cls._metadata_parts(data.version)
        if metadata_parts is None:
            return None
        metadata, parts = metadata_parts
        if data.chunk.ordinal < 0 or data.chunk.ordinal >= len(parts):
            return None
        part, locator = parts[data.chunk.ordinal]
        try:
            if content_hash(part) != content_hash(data.chunk.content):
                return None
        except ValueError:
            return None
        return metadata, locator if isinstance(locator, dict) else {}

    def _locate(
        self,
        data: _AuthoritativeCitationData,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        metadata_locator = self._metadata_locator(data)
        if metadata_locator is None:
            return self._fallback(data)
        metadata, locator = metadata_locator
        source_type = data.item.source_type

        if source_type == "video" and locator.get("kind") in {
            "video",
            "video_chapter",
            "video_keyframe",
        }:
            located = self._locate_video(data, metadata, locator)
            if located is not None:
                return located
            return self._fallback(data)

        if locator.get("kind") == "pdf" and source_type == "pdf":
            page = _positive_int(locator.get("page"))
            page_count = _positive_int(metadata.get("page_count"))
            page_label = locator.get("page_label")
            if page_label is not None and (
                not isinstance(page_label, str) or not page_label or page_label != page_label.strip()
            ):
                return self._fallback(data)
            artifact = self._artifact(
                metadata,
                data.artifacts,
                key="source_artifact_id",
                artifact_type="original_input",
                media_types={PDF_MEDIA_TYPE},
            )
            if page is None or page_count is None or page > page_count or artifact is None:
                return self._fallback(data)
            normalized = {"kind": "pdf", "page": page}
            if page_label is not None:
                normalized["page_label"] = page_label
            return (
                "exact",
                normalized,
                {"kind": "artifact", "artifact_id": artifact.id, "page": page},
            )

        if locator.get("kind") == "docx" and source_type == "docx":
            element = locator.get("element")
            paragraph_count = _positive_int(metadata.get("paragraph_count"))
            table_count = metadata.get("table_count")
            table_row_counts = metadata.get("table_row_counts")
            heading_count = metadata.get("heading_count")
            heading_paragraphs = metadata.get("heading_paragraphs")
            if (
                paragraph_count is None
                or not isinstance(table_count, int)
                or isinstance(table_count, bool)
                or table_count < 0
                or not isinstance(table_row_counts, list)
                or len(table_row_counts) != table_count
                or any(_positive_int(row_count) is None for row_count in table_row_counts)
                or not isinstance(heading_count, int)
                or isinstance(heading_count, bool)
                or heading_count < 0
                or not isinstance(heading_paragraphs, list)
                or len(heading_paragraphs) != heading_count
                or any(_positive_int(value) is None for value in heading_paragraphs)
            ):
                return self._fallback(data)
            if "heading_path" not in locator:
                return self._fallback(data)
            heading_path = _heading_path(
                locator.get("heading_path"), allow_empty=element != "heading"
            )
            valid = heading_path is not None
            if element == "heading":
                heading_paragraph = _positive_int(locator.get("paragraph"))
                valid = (
                    valid
                    and heading_paragraph is not None
                    and _positive_int(locator.get("heading_level")) in range(1, 7)
                    and heading_paragraph <= paragraph_count
                    and heading_paragraph in heading_paragraphs
                )
            elif element == "paragraph":
                paragraph = _positive_int(locator.get("paragraph"))
                valid = valid and paragraph is not None and paragraph <= paragraph_count
            elif element == "table_row":
                table = _positive_int(locator.get("table"))
                row = _positive_int(locator.get("row"))
                valid = (
                    valid
                    and table is not None
                    and row is not None
                    and table <= table_count
                    and row <= table_row_counts[table - 1]
                )
            else:
                valid = False
            artifact = self._artifact(
                metadata,
                data.artifacts,
                key="source_artifact_id",
                artifact_type="original_input",
                media_types={DOCX_MEDIA_TYPE},
            )
            if not valid or artifact is None:
                return self._fallback(data)
            normalized: dict[str, Any] = {
                "kind": "docx",
                "element": element,
                "heading_path": heading_path,
            }
            for key in ("heading_level", "paragraph", "table", "row"):
                if key in locator:
                    normalized[key] = locator[key]
            return "exact", normalized, {"kind": "artifact", "artifact_id": artifact.id}

        if locator.get("kind") == "webpage" and source_type == "webpage":
            metadata_url = _safe_url(metadata.get("url"))
            locator_url = _safe_url(locator.get("url"))
            original = self._artifact(
                metadata,
                data.artifacts,
                key="source_artifact_id",
                artifact_type="original_input",
                media_types={"text/uri-list"},
            )
            snapshot = self._artifact(
                metadata,
                data.artifacts,
                key="snapshot_artifact_id",
                artifact_type="web_snapshot",
                media_types=WEB_MEDIA_TYPES,
            )
            original_locator = _json_object(original.source_locator) if original else None
            snapshot_locator = _json_object(snapshot.source_locator) if snapshot else None
            requested_url = _safe_url(original_locator.get("url")) if original_locator else None
            final_url = (
                _safe_url(snapshot_locator.get("url"))
                if snapshot_locator
                else None
            )
            if (
                metadata_url is None
                or locator_url != metadata_url
                or original is None
                or snapshot is None
                or original_locator is None
                or original_locator.get("kind") != "url_request"
                or requested_url is None
                or snapshot_locator is None
                or snapshot_locator.get("kind") != "web_snapshot"
                or final_url != metadata_url
            ):
                return self._fallback(data)
            element = locator.get("element")
            allowed_elements = {"heading", "p", "li", "blockquote", "pre", "dt", "dd"}
            if not isinstance(element, str) or element not in allowed_elements:
                return self._fallback(data)
            if element == "heading":
                heading_count = _positive_int(metadata.get("heading_count"))
                heading_level = _positive_int(locator.get("heading_level"))
                if heading_count is None or heading_level not in range(1, 7):
                    return self._fallback(data)
            normalized = {"kind": "webpage", "url": metadata_url, "element": element}
            if "heading_path" in locator:
                heading_path = _heading_path(locator.get("heading_path"), allow_empty=True)
                if heading_path is None:
                    return self._fallback(data)
                normalized["heading_path"] = heading_path
            return "exact", normalized, {"kind": "url", "url": metadata_url}

        if locator.get("kind") == "obsidian":
            path = safe_relative_path(locator.get("path"))
            binding_path = self._binding_path(
                data.item,
                data.binding,
                require_synced=True,
                expected_content_hash=data.version.content_hash,
            )
            if path is not None and binding_path == path:
                return (
                    "exact",
                    {"kind": "obsidian", "path": path},
                    {"kind": "obsidian", "item_id": data.item.id},
                )
        return self._fallback(data)

    async def build(self, chunks: Sequence[RetrievedChunk]) -> list[BuiltCitation]:
        if not chunks:
            return []
        authoritative = await self._authoritative_data(chunks)
        if len(authoritative) != len(set(chunk.chunk_id for chunk in chunks)):
            raise CitationBuildError("检索证据已不再属于当前发布版本")

        citations: list[BuiltCitation] = []
        for index, retrieved in enumerate(chunks, start=1):
            data = authoritative.get(retrieved.chunk_id)
            if data is None:
                raise CitationBuildError("检索证据已不再属于当前发布版本")
            try:
                authoritative_hash = content_hash(data.chunk.content)
                if (
                    retrieved.knowledge_item_id != data.item.id
                    or retrieved.content_version_id != data.version.id
                    or retrieved.ordinal != data.chunk.ordinal
                    or data.chunk.source_type != data.item.source_type
                    or data.chunk.content_hash != authoritative_hash
                    or data.version.content_hash != content_hash(data.version.body)
                    or data.item.content_hash != data.version.content_hash
                    or content_hash(retrieved.content) != authoritative_hash
                ):
                    raise CitationBuildError("检索证据快照无效")
            except ValueError as error:
                raise CitationBuildError("检索证据快照无效") from error
            status, locator, target = self._locate(data)
            content_digest = authoritative_hash
            citations.append(
                BuiltCitation(
                    citation_id=f"C{index}",
                    chunk_id=data.chunk.id,
                    knowledge_item_id=data.item.id,
                    content_version_id=data.version.id,
                    item_title=data.version.title,
                    version_no=data.version.version_no,
                    source_type=data.item.source_type,
                    excerpt=_excerpt(data.chunk.content),
                    chunk_content_hash=content_digest,
                    locator_status=status,
                    locator=locator,
                    target=target,
                    retrieval={
                        "matched_by": list(retrieved.matched_by),
                        "fts_rank": retrieved.fts_rank,
                        "vector_rank": retrieved.vector_rank,
                        "fts_score": retrieved.fts_score,
                        "vector_score": retrieved.vector_score,
                        "rrf_score": retrieved.rrf_score,
                        "rerank_score": retrieved.rerank_score,
                    },
                )
            )
        return citations
