import asyncio
import json
import structlog
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.core.safety import redact_sensitive_text
from app.db.models import (
    Collection,
    CollectionItem,
    ContentVersion,
    KnowledgeItem,
    NoteBinding,
    ProcessingJob,
    KnowledgeItemTag,
    SourceArtifact,
    Tag,
)
from app.ingestion.fetcher import SourceFetcher, UnsafeUrlError
from app.ingestion.parsers import parse_source
from app.ingestion.types import ParsedSource, SourceBlock
from app.obsidian.markdown import ObsidianVault, StagedWrite, parse_note, render_note
from app.providers.models import DraftProvider, EmbeddingProvider
from app.providers.video import (
    ASRProvider,
    AudioExtractor,
    FfmpegAudioExtractor,
    OCRProvider,
    SceneDetector,
    VideoSourceProvider,
    VisionProvider,
    YtDlpDownloader,
    YtDlpVideoSourceProvider,
)
from app.services.artifacts import ArtifactStore, StoredArtifact
from app.services.content import content_hash, default_title, normalize_content
from app.services.indexing import IndexService
from app.services.video import VideoService
from app.services.vector_store import QdrantLocalStore
from app.schemas.collections import normalize_collection_names
from app.schemas.tags import normalize_tag_names, normalize_tag_text


logger = structlog.get_logger("stage2")


def _safe_tag_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    accepted: list[str] = []
    for candidate in value[:50]:
        try:
            accepted.append(normalize_tag_text(candidate))
        except ValueError:
            continue
    try:
        return normalize_tag_names(accepted)
    except ValueError:
        return []


def _safe_collection_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    accepted: list[str] = []
    for candidate in value[:50]:
        try:
            accepted.extend(normalize_collection_names([candidate]))
        except ValueError:
            continue
    try:
        return normalize_collection_names(accepted)
    except ValueError:
        return []


def _json_name_list(raw: str | None, *, kind: Literal["tag", "collection"]) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return _safe_tag_names(value) if kind == "tag" else _safe_collection_names(value)


class Stage2Service:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        draft_provider: DraftProvider,
        embedding_provider: EmbeddingProvider | None,
        video_provider: VideoSourceProvider | None = None,
        asr_provider: ASRProvider | None = None,
        audio_extractor: AudioExtractor | None = None,
        scene_detector: SceneDetector | None = None,
        vision_provider: VisionProvider | None = None,
        ocr_provider: OCRProvider | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.artifacts = ArtifactStore(settings.artifact_root)
        self.draft_provider = draft_provider
        self.mutation_lock = asyncio.Lock()
        self._initial_vector_reconciliation_complete = False
        self.embedding_provider = embedding_provider
        self.vector_store = QdrantLocalStore(settings.qdrant_path, settings.embedding_dimensions)
        self.source_fetcher = SourceFetcher(
            max_bytes=settings.source_max_bytes,
            timeout=settings.source_fetch_timeout,
            max_redirects=settings.source_max_redirects,
        )
        resolved_video_provider = video_provider or YtDlpVideoSourceProvider(
            YtDlpDownloader(
                settings.artifact_root,
                executable=settings.video_ytdlp_executable,
                url_validator=self.source_fetcher.validate,
            )
        )
        self.video_service = VideoService(
            settings,
            session_factory,
            self.artifacts,
            video_provider=resolved_video_provider,
            asr_provider=asr_provider,
            audio_extractor=audio_extractor
            if audio_extractor is not None
            else FfmpegAudioExtractor(
                settings.artifact_root,
                executable=settings.video_ffmpeg_executable,
            ),
            scene_detector=scene_detector,
            vision_provider=vision_provider,
            ocr_provider=ocr_provider,
            url_validator=self.source_fetcher.validate,
        )

    async def submit_video(
        self,
        url: str,
        title: str | None,
        language: str | None,
        idempotency_key: str | None,
        enable_vision: bool,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        return await self.video_service.submit_video(
            url, title, language, idempotency_key, enable_vision
        )

    async def process_video(self, job: ProcessingJob) -> dict[str, object]:
        return await self.video_service.process_video(job)

    async def reprocess_video(self, item_id: str) -> tuple[KnowledgeItem, ProcessingJob]:
        return await self.video_service.reprocess(item_id)

    async def cleanup_video_artifacts(self) -> dict[str, int]:
        return await self.video_service.cleanup_due()

    def vault(self) -> ObsidianVault:
        if self.settings.vault_root is None:
            raise ApplicationError(409, "vault_not_configured", "尚未配置 Obsidian Vault")
        return ObsidianVault(self.settings.vault_root, self.settings.managed_vault_dir)

    async def submit_file(
        self,
        content: bytes,
        filename: str | None,
        media_type: str | None,
        title: str | None,
        idempotency_key: str | None,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        if len(content) > self.settings.source_max_bytes:
            raise ApplicationError(413, "source_too_large", "来源文件超过大小限制")
        suffix = Path(filename or "").suffix.lower()
        source_by_suffix = {
            ".pdf": ("pdf", "application/pdf"),
            ".docx": (
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        }
        source_type, default_media_type = source_by_suffix.get(suffix, (None, None))
        if source_type is None:
            raise ApplicationError(422, "unsupported_file_type", "仅支持 PDF 和 DOCX 文件")
        stored = self.artifacts.put_bytes(content, suffix)
        source_locator = json.dumps(
            {
                "kind": "file_upload",
                "filename": Path(filename or f"source{suffix}").name,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return await self._submit_artifact(
            stored,
            source_type=source_type,
            media_type=media_type or default_media_type,
            title=title,
            fallback_title=Path(filename or "").stem or f"{source_type} 来源",
            idempotency_key=idempotency_key,
            source_locator=source_locator,
        )

    async def submit_url(
        self,
        url: str,
        title: str | None,
        idempotency_key: str | None,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        try:
            self.source_fetcher.validate(url)
        except UnsafeUrlError as error:
            raise ApplicationError(422, "unsafe_url", str(error)) from error
        stored = self.artifacts.put_text(url, ".url")
        source_locator = json.dumps(
            {"kind": "url_request", "url": url},
            ensure_ascii=False,
            sort_keys=True,
        )
        return await self._submit_artifact(
            stored,
            source_type="webpage",
            media_type="text/uri-list",
            title=title,
            fallback_title=urlsplit(url).hostname or "网页来源",
            idempotency_key=idempotency_key,
            source_locator=source_locator,
            payload_extra={"url": url},
        )

    async def _submit_artifact(
        self,
        stored: StoredArtifact,
        *,
        source_type: str,
        media_type: str,
        title: str | None,
        fallback_title: str,
        idempotency_key: str | None,
        source_locator: str,
        payload_extra: dict[str, object] | None = None,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        async with self.session_factory() as session, session.begin():
            if idempotency_key:
                existing_job_result = await session.execute(
                    select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
                )
                existing_job = existing_job_result.scalar_one_or_none()
                if existing_job is not None:
                    existing_payload = json.loads(existing_job.payload_json)
                    existing_item = await session.get(
                        KnowledgeItem, existing_payload.get("item_id")
                    )
                    if existing_item is None or existing_item.content_hash != stored.content_hash:
                        raise ApplicationError(
                            409,
                            "idempotency_conflict",
                            "同一幂等键已用于不同内容",
                        )
                    return existing_item, existing_job, True
            duplicate_result = await session.execute(
                select(KnowledgeItem)
                .where(
                    KnowledgeItem.content_hash == stored.content_hash,
                    KnowledgeItem.deleted_at.is_(None),
                )
                .order_by(KnowledgeItem.created_at)
                .limit(1)
            )
            duplicate = duplicate_result.scalar_one_or_none()
            if duplicate is not None:
                job_result = await session.execute(
                    select(ProcessingJob)
                    .where(ProcessingJob.payload_json.like(f'%"item_id": "{duplicate.id}"%'))
                    .order_by(ProcessingJob.created_at.desc())
                    .limit(1)
                )
                existing_job = job_result.scalar_one_or_none()
                if existing_job is None:
                    existing_job = ProcessingJob(
                        kind="ingest_source",
                        state="succeeded",
                        stage="deduplicated",
                        progress=1.0,
                        payload_json=json.dumps(
                            {"item_id": duplicate.id}, ensure_ascii=False
                        ),
                        result_json=json.dumps(
                            {"item_id": duplicate.id}, ensure_ascii=False
                        ),
                    )
                    session.add(existing_job)
                return duplicate, existing_job, True

            item = KnowledgeItem(
                title=(title or fallback_title)[:300],
                source_type=source_type,
                status="processing",
                content_hash=stored.content_hash,
            )
            session.add(item)
            await session.flush()
            artifact = SourceArtifact(
                knowledge_item_id=item.id,
                artifact_type="original_input",
                media_type=media_type,
                relative_path=stored.relative_path,
                content_hash=stored.content_hash,
                byte_size=stored.byte_size,
                source_locator=source_locator,
            )
            session.add(artifact)
            await session.flush()
            payload: dict[str, object] = {
                "item_id": item.id,
                "artifact_id": artifact.id,
                "source_type": source_type,
                "title_provided": title is not None,
            }
            if payload_extra:
                payload.update(payload_extra)
            job = ProcessingJob(
                kind="ingest_source",
                payload_json=json.dumps(payload, ensure_ascii=False),
                idempotency_key=idempotency_key,
            )
            session.add(job)
            await session.flush()
            return item, job, False

    async def submit_text(
        self,
        content: str,
        source_type: str,
        title: str | None,
        idempotency_key: str | None,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        normalized = normalize_content(content)
        digest = content_hash(normalized)
        stored = self.artifacts.put_text(normalized, ".md" if source_type == "markdown" else ".txt")
        async with self.session_factory() as session, session.begin():
            if idempotency_key:
                existing_job_result = await session.execute(
                    select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
                )
                existing_job = existing_job_result.scalar_one_or_none()
                if existing_job is not None:
                    existing_payload = json.loads(existing_job.payload_json)
                    existing_item = await session.get(
                        KnowledgeItem, existing_payload.get("item_id")
                    )
                    if existing_item is None or existing_item.content_hash != digest:
                        raise ApplicationError(
                            409,
                            "idempotency_conflict",
                            "同一幂等键已用于不同内容",
                        )
                    return existing_item, existing_job, True
            duplicate_result = await session.execute(
                select(KnowledgeItem)
                .where(
                    KnowledgeItem.content_hash == digest,
                    KnowledgeItem.deleted_at.is_(None),
                )
                .order_by(KnowledgeItem.created_at)
                .limit(1)
            )
            duplicate = duplicate_result.scalar_one_or_none()
            if duplicate is not None:
                job_result = await session.execute(
                    select(ProcessingJob)
                    .where(ProcessingJob.payload_json.like(f'%"item_id": "{duplicate.id}"%'))
                    .order_by(ProcessingJob.created_at.desc())
                    .limit(1)
                )
                existing_job = job_result.scalar_one_or_none()
                if existing_job is None:
                    existing_job = ProcessingJob(
                        kind="ingest_text",
                        state="succeeded",
                        stage="deduplicated",
                        progress=1.0,
                        payload_json=json.dumps({"item_id": duplicate.id}),
                        result_json=json.dumps({"item_id": duplicate.id}),
                    )
                    session.add(existing_job)
                return duplicate, existing_job, True

            item = KnowledgeItem(
                title=(title or default_title(normalized))[:300],
                source_type=source_type,
                status="processing",
                content_hash=digest,
            )
            session.add(item)
            await session.flush()
            artifact = SourceArtifact(
                knowledge_item_id=item.id,
                artifact_type="original_input",
                media_type="text/markdown" if source_type == "markdown" else "text/plain",
                relative_path=stored.relative_path,
                content_hash=stored.content_hash,
                byte_size=stored.byte_size,
                source_locator="inbox",
            )
            session.add(artifact)
            await session.flush()
            job = ProcessingJob(
                kind="ingest_text",
                payload_json=json.dumps(
                    {
                        "item_id": item.id,
                        "artifact_id": artifact.id,
                        "source_type": source_type,
                        "title_provided": title is not None,
                    },
                    ensure_ascii=False,
                ),
                idempotency_key=idempotency_key,
            )
            session.add(job)
            await session.flush()
            return item, job, False

    async def process_ingestion(self, job: ProcessingJob) -> dict[str, object]:
        payload = json.loads(job.payload_json)
        async with self.session_factory() as session:
            item = await session.get(KnowledgeItem, payload["item_id"])
            artifact = await session.get(SourceArtifact, payload["artifact_id"])
        if item is None or artifact is None:
            raise RuntimeError("采集记录不完整")

        source_type = str(payload.get("source_type") or item.source_type)
        snapshot: SourceArtifact | None = None
        if source_type == "webpage":
            url = payload.get("url")
            if not isinstance(url, str) or not url:
                raise RuntimeError("网页来源缺少 URL")
            try:
                fetched = await self.source_fetcher.fetch(url)
            except UnsafeUrlError as error:
                raise ApplicationError(422, "unsafe_url", str(error)) from error
            stored_snapshot = self.artifacts.put_bytes(fetched.content, ".html")
            async with self.session_factory() as session, session.begin():
                item = await session.get(KnowledgeItem, item.id)
                if item is None:
                    raise RuntimeError("知识条目不存在")
                snapshot = SourceArtifact(
                    knowledge_item_id=item.id,
                    artifact_type="web_snapshot",
                    media_type=fetched.media_type,
                    relative_path=stored_snapshot.relative_path,
                    content_hash=stored_snapshot.content_hash,
                    byte_size=stored_snapshot.byte_size,
                    source_locator=json.dumps(
                        {
                            "kind": "web_snapshot",
                            "requested_url": fetched.requested_url,
                            "url": fetched.final_url,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                session.add(snapshot)
                await session.flush()
            parsed = parse_source(
                source_type,
                fetched.content,
                url=fetched.final_url,
            )
        elif source_type in {"text", "markdown"}:
            content = self.artifacts.read_text(artifact.relative_path)
            body = normalize_content(content)
            block = SourceBlock(
                body.rstrip("\n"),
                {"kind": source_type, "source_locator": artifact.source_locator or "inbox"},
            )
            parsed = ParsedSource(
                source_type=source_type,
                media_type=artifact.media_type,
                title=default_title(body),
                body=body,
                blocks=(block,),
                metadata={
                    "source_type": source_type,
                    "media_type": artifact.media_type,
                    "title": default_title(body),
                    "segments": [{"text": block.text, "locator": block.locator}],
                },
            )
        else:
            parsed = parse_source(
                source_type,
                self.artifacts.read_bytes(artifact.relative_path),
            )

        title_provided = bool(payload.get("title_provided"))
        draft_title = item.title if title_provided else parsed.title
        draft = await self.draft_provider.create_draft(draft_title, parsed.body)
        async with self.mutation_lock:
            async with self.session_factory() as session, session.begin():
                item = await session.get(KnowledgeItem, item.id)
                if item is None:
                    raise RuntimeError("知识条目不存在")
                next_no = await self._next_version_no(session, item.id)
                version = ContentVersion(
                    knowledge_item_id=item.id,
                    version_no=next_no,
                    source_kind="draft",
                    title=draft.title[:300],
                    body=normalize_content(draft.body),
                    content_hash=content_hash(draft.body),
                    summary=draft.summary,
                    suggested_tags_json=json.dumps(
                        _safe_tag_names(draft.suggested_tags), ensure_ascii=False
                    ),
                    suggested_collections_json=json.dumps(
                        _safe_collection_names(draft.suggested_collections),
                        ensure_ascii=False,
                    ),
                    prompt_version=draft.prompt_version,
                    source_metadata_json=json.dumps(
                        {
                            **parsed.metadata,
                            "source_artifact_id": artifact.id,
                            **(
                                {"snapshot_artifact_id": snapshot.id}
                                if snapshot is not None
                                else {}
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
                session.add(version)
                await session.flush()
                published_current = (
                    item.status == "published"
                    and item.current_content_version_id is not None
                )
                if published_current:
                    # A reprocess result is another review candidate.  The
                    # published/current version remains authoritative until
                    # the candidate passes both HITL gates.
                    item.pending_content_version_id = version.id
                else:
                    if not title_provided:
                        item.title = version.title
                    item.status = "pending_review"
                    item.current_content_version_id = version.id
                    item.pending_content_version_id = None
                item.updated_at = datetime.now(timezone.utc)
                result: dict[str, object] = {
                    "item_id": item.id,
                    "content_version_id": version.id,
                    "source_type": source_type,
                }
                if snapshot is not None:
                    result["snapshot_artifact_id"] = snapshot.id
                return result

    async def _next_version_no(self, session: AsyncSession, item_id: str) -> int:
        result = await session.execute(
            select(func.max(ContentVersion.version_no)).where(
                ContentVersion.knowledge_item_id == item_id
            )
        )
        return int(result.scalar_one() or 0) + 1

    async def update_pending_review(
        self,
        item_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        summary: str | None = None,
        suggested_tags: list[str] | None = None,
        suggested_collections: list[str] | None = None,
    ) -> KnowledgeItem:
        """Apply reviewer edits to the candidate version before graph resume.

        The candidate row keeps its identifier, so the persisted graph
        checkpoint still points at the exact version that will be published.
        No organization relation is made official until the publish service
        commits the reviewed Markdown version.
        """

        async with self.mutation_lock:
            async with self.session_factory() as session, session.begin():
                item = await self._item(session, item_id)
                if item.status not in {"pending_review", "published"}:
                    raise ApplicationError(409, "invalid_item_state", "条目不在待审核状态")
                if item.status == "published" and item.pending_content_version_id is None:
                    raise ApplicationError(409, "invalid_item_state", "条目不在待审核状态")
                version = await self._version(session, item)
                if title is not None:
                    version.title = title[:300]
                if body is not None:
                    version.body = normalize_content(body)
                    version.content_hash = content_hash(version.body)
                if summary is not None:
                    version.summary = summary.strip() or None
                if suggested_tags is not None:
                    version.suggested_tags_json = json.dumps(
                        _safe_tag_names(suggested_tags), ensure_ascii=False
                    )
                if suggested_collections is not None:
                    version.suggested_collections_json = json.dumps(
                        _safe_collection_names(suggested_collections),
                        ensure_ascii=False,
                    )
                if item.status != "published" and item.current_content_version_id == version.id:
                    item.title = version.title
                item.updated_at = datetime.now(timezone.utc)
                await session.flush()
                return item

    async def review(self, item_id: str) -> KnowledgeItem:
        async with self.session_factory() as session, session.begin():
            item = await self._item(session, item_id)
            if item.status == "published" and item.pending_content_version_id is None:
                raise ApplicationError(409, "invalid_item_state", "条目不在待审核状态")
            if item.status == "reviewed":
                return item
            if item.status not in {"pending_review", "published"}:
                raise ApplicationError(409, "invalid_item_state", "条目不在待审核状态")
            if item.status != "published":
                item.status = "reviewed"
            item.updated_at = datetime.now(timezone.utc)
            await session.flush()
            return item

    async def abandon_pending_decision(
        self, item_id: str, decision: Literal["reject", "cancel"]
    ) -> KnowledgeItem:
        """Close an unapproved candidate without changing user-owned content.

        A rejected or cancelled intake is kept as a failed, non-pending item so
        the workflow/job and item state agree.  A published item keeps its
        authoritative current version while discarding only the unapproved
        pending candidate.  Reprocess is the explicit path for a new review
        cycle.
        """
        if decision not in {"reject", "cancel"}:
            raise ApplicationError(400, "invalid_review_decision", "审核决策无效")
        async with self.mutation_lock:
            async with self.session_factory() as session, session.begin():
                item = await self._item(session, item_id)
                if item.status not in {"pending_review", "reviewed", "published"}:
                    raise ApplicationError(409, "invalid_item_state", "条目不在可结束状态")
                item.pending_content_version_id = None
                if item.status != "published":
                    item.status = "failed"
                item.updated_at = datetime.now(timezone.utc)
                await session.flush()
                return item

    async def patch_item(
        self,
        item_id: str,
        title: str | None,
        body: str | None,
        expected_content_hash: str | None,
    ) -> KnowledgeItem:
        async with self.mutation_lock:
            item = await self._patch_item(item_id, title, body, expected_content_hash)
            if item.status == "published" and item.current_content_version_id:
                self.vector_store.delete_item_except_version(
                    item.id, item.current_content_version_id
                )
            return item

    async def _patch_item(
        self,
        item_id: str,
        title: str | None,
        body: str | None,
        expected_content_hash: str | None,
    ) -> KnowledgeItem:
        async with self.session_factory() as session, session.begin():
            item = await self._item(session, item_id)
            if item.status == "published" and item.pending_content_version_id is None:
                return await self._patch_published(
                    session, item, title, body, expected_content_hash
                )
            if item.status not in {"pending_review", "reviewed", "published"}:
                raise ApplicationError(409, "invalid_item_state", "当前状态不可编辑")
            current = await self._version(session, item)
            next_body = normalize_content(body if body is not None else current.body)
            version = ContentVersion(
                knowledge_item_id=item.id,
                version_no=await self._next_version_no(session, item.id),
                source_kind="draft",
                title=(title or current.title)[:300],
                body=next_body,
                content_hash=content_hash(next_body),
                summary=current.summary,
                suggested_tags_json=current.suggested_tags_json,
                suggested_collections_json=current.suggested_collections_json,
                prompt_version=current.prompt_version,
                source_metadata_json=current.source_metadata_json,
            )
            session.add(version)
            await session.flush()
            pending_target = item.source_type == "video" or item.pending_content_version_id is not None
            if item.current_content_version_id is None:
                item.title = version.title
            if pending_target:
                item.pending_content_version_id = version.id
            else:
                item.current_content_version_id = version.id
                item.content_hash = version.content_hash
            if item.current_content_version_id is None:
                item.status = "pending_review"
            item.updated_at = datetime.now(timezone.utc)
            return item

    async def _patch_published(
        self,
        session: AsyncSession,
        item: KnowledgeItem,
        title: str | None,
        body: str | None,
        expected_content_hash: str | None,
    ) -> KnowledgeItem:
        binding_result = await session.execute(
            select(NoteBinding).where(NoteBinding.knowledge_item_id == item.id)
        )
        binding = binding_result.scalar_one_or_none()
        if binding is None:
            raise ApplicationError(409, "note_binding_missing", "发布条目缺少笔记绑定")
        vault = self.vault()
        current = await self._version(session, item)
        disk_note = vault.read(binding.relative_path)
        disk_hash = content_hash(disk_note.body)
        if not expected_content_hash or expected_content_hash != disk_hash:
            raise ApplicationError(
                409,
                "content_conflict",
                "Obsidian 内容已变化，请重新载入后再编辑",
                {"current_content_hash": disk_hash},
            )
        next_title = (title or item.title)[:300]
        next_body = normalize_content(body if body is not None else disk_note.body)
        existing_collections = await self._collection_names(session, item.id)
        collections, tags = self._frontmatter_relation_names(disk_note.metadata)
        # Older managed notes may not have a collections key.  Keep their
        # confirmed relation in that case; an explicit empty list means the
        # user intentionally removed every collection.
        if "collections" not in disk_note.metadata:
            collections = existing_collections
        collection_ids = await self._collection_ids_for_names(session, collections)
        tag_ids = await self._tag_ids_for_names(session, tags)
        raw = render_note(
            zhiliu_id=binding.zhiliu_id,
            source_type=item.source_type,
            title=next_title,
            body=next_body,
            status="reviewed",
            created_at=item.created_at,
            updated_at=datetime.now(timezone.utc),
            tags=tags,
            collections=collections,
            source_url=self._source_url(current.source_metadata_json),
        )
        vault.atomic_write(binding.relative_path, raw)
        source_metadata_json = self._vault_source_metadata(
            item,
            next_body,
            binding.relative_path,
            source_url=self._source_url(current.source_metadata_json),
        )
        await self._apply_vault_version(
            session,
            item,
            binding,
            next_title,
            next_body,
            binding.relative_path,
            source_metadata_json=source_metadata_json,
        )
        await self._replace_collection_relations(session, item.id, collection_ids)
        await self._replace_tag_relations(session, item.id, tag_ids)
        return item

    async def publish(self, item_id: str) -> KnowledgeItem:
        async with self.mutation_lock:
            item = await self._publish(item_id)
            if item.current_content_version_id:
                try:
                    self.vector_store.delete_item_except_version(
                        item.id, item.current_content_version_id
                    )
                except Exception as error:
                    # SQLite current_content_version_id is authoritative.  A
                    # stale vector can never become answer evidence, and its
                    # cleanup can be retried on a later reconciliation.
                    logger.error(
                        "published_vector_cleanup_failed",
                        error_type=type(error).__name__,
                    )
            return item

    async def _publish(self, item_id: str) -> KnowledgeItem:
        if self.embedding_provider is None:
            raise ApplicationError(
                409,
                "embedding_not_configured",
                "发布前必须配置 Embedding capability，以完成 Qdrant 索引",
            )
        vault = self.vault()
        staged: StagedWrite | None = None
        old_raw: bytes | None = None
        old_exists = False
        vault_swap_attempted = False
        candidate_version_id: str | None = None
        try:
            async with self.session_factory() as session, session.begin():
                item = await self._item(session, item_id)
                if item.status == "published" and item.pending_content_version_id is None:
                    return item
                if item.status not in {"reviewed", "published"}:
                    raise ApplicationError(409, "invalid_item_state", "条目必须先审核再发布")
                current = await self._version(session, item)
                existing_collections = await self._collection_names(session, item.id)
                suggested_collections = _json_name_list(
                    current.suggested_collections_json, kind="collection"
                )
                collections = suggested_collections or existing_collections
                existing_tags = await self._tag_names(session, item.id)
                suggested_tags = _json_name_list(current.suggested_tags_json, kind="tag")
                tags = suggested_tags or existing_tags
                collection_ids = await self._collection_ids_for_names(session, collections)
                tag_ids = await self._tag_ids_for_names(session, tags)
                binding_result = await session.execute(
                    select(NoteBinding).where(NoteBinding.knowledge_item_id == item.id)
                )
                binding = binding_result.scalar_one_or_none()
                relative_path = (
                    binding.relative_path
                    if binding is not None
                    else vault.publish_path(item.title, item.id)
                )
                raw = render_note(
                    zhiliu_id=item.id,
                    source_type=item.source_type,
                    title=current.title,
                    body=current.body,
                    status="reviewed",
                    created_at=item.created_at,
                    updated_at=datetime.now(timezone.utc),
                    tags=tags,
                    collections=collections,
                    source_url=self._source_url(current.source_metadata_json),
                )

                target = vault.resolve(relative_path)
                if target.exists() and not target.is_file():
                    raise OSError("Vault 目标不可写")
                old_exists = target.is_file()
                if old_exists:
                    old_raw = target.read_bytes()

                # The candidate is first written to a same-directory temp file.
                # It is not visible to Obsidian or readers while indexes are
                # prepared inside the SQLite transaction.
                staged = vault.stage_write(relative_path, raw)
                version = await self._new_vault_version(
                    session,
                    item,
                    current.title,
                    current.body,
                    relative_path,
                    source_metadata_json=current.source_metadata_json,
                    summary=current.summary,
                    suggested_tags_json=current.suggested_tags_json,
                    suggested_collections_json=current.suggested_collections_json,
                    prompt_version=current.prompt_version,
                )
                candidate_version_id = version.id
                index = IndexService(self.vector_store, self.embedding_provider)
                prepared = await index.prepare_version(item, version, relative_path)
                await index.apply_prepared(session, item, version, prepared)

                # Only after embedding, Qdrant upsert and SQLite chunk/FTS
                # changes have prepared successfully does the final file swap.
                # Mark the swap as attempted before calling it.  A filesystem
                # adapter can report an error after os.replace has completed;
                # compensation must cover that ambiguous outcome too.
                vault_swap_attempted = True
                vault.commit_staged(staged)
                written = vault.read(relative_path)
                if content_hash(written.body) != content_hash(current.body):
                    raise OSError("Vault 落盘校验失败")
                digest = content_hash(written.body)
                if binding is None:
                    binding = NoteBinding(
                        knowledge_item_id=item.id,
                        zhiliu_id=item.id,
                        relative_path=relative_path,
                        content_hash=digest,
                        last_written_hash=digest,
                        sync_state="synced",
                        last_synced_at=datetime.now(timezone.utc),
                    )
                    session.add(binding)
                    await session.flush()
                self._activate_vault_version(item, binding, version, relative_path, digest)
                await self._replace_collection_relations(session, item.id, collection_ids)
                await self._replace_tag_relations(session, item.id, tag_ids)
                item.pending_content_version_id = None
            return item
        except BaseException:
            if staged is not None:
                try:
                    vault.discard_staged(staged)
                except (OSError, ValueError):
                    logger.error(
                        "publish_staged_vault_cleanup_failed",
                        error_type="staged_cleanup",
                    )
            if vault_swap_attempted:
                try:
                    if old_exists:
                        if old_raw is None:
                            raise OSError("Vault 旧正文快照缺失")
                        restore = vault.stage_bytes(relative_path, old_raw)
                        try:
                            vault.commit_staged(restore)
                        finally:
                            vault.discard_staged(restore)
                    else:
                        vault.remove(relative_path)
                except (OSError, ValueError) as error:
                    logger.error(
                        "publish_vault_compensation_failed",
                        error_type=type(error).__name__,
                    )
            if candidate_version_id is not None:
                self._discard_candidate_vectors(candidate_version_id)
            raise

    @staticmethod
    async def _collection_names(
        session: AsyncSession, item_id: str
    ) -> list[str]:
        result = await session.execute(
            select(Collection.name)
            .join(CollectionItem, CollectionItem.collection_id == Collection.id)
            .where(CollectionItem.knowledge_item_id == item_id)
            .order_by(func.lower(Collection.name), Collection.name)
        )
        try:
            return normalize_collection_names([name for name in result.scalars() if isinstance(name, str)])
        except ValueError as error:
            raise ApplicationError(
                409,
                "collection_invalid_state",
                "合集关系包含不允许的内容",
            ) from error

    @staticmethod
    async def _tag_names(session: AsyncSession, item_id: str) -> list[str]:
        result = await session.execute(
            select(Tag.name)
            .join(KnowledgeItemTag, KnowledgeItemTag.tag_id == Tag.id)
            .where(KnowledgeItemTag.knowledge_item_id == item_id)
            .order_by(Tag.normalized_name, Tag.name)
        )
        try:
            return normalize_tag_names(
                [name for name in result.scalars() if isinstance(name, str)]
            )
        except ValueError as error:
            raise ApplicationError(
                409,
                "tag_invalid_state",
                "标签关系包含不允许的内容",
            ) from error

    async def get_item_organization(self, item_id: str) -> tuple[list[str], list[str]]:
        """Return confirmed tags and collections for a safe item projection."""

        async with self.session_factory() as session:
            item = await self._item(session, item_id)
            return (
                await self._tag_names(session, item.id),
                await self._collection_names(session, item.id),
            )

    @staticmethod
    async def _tag_ids_for_names(
        session: AsyncSession, tag_names: Sequence[str]
    ) -> set[str]:
        normalized_names = normalize_tag_names(list(tag_names))
        folded_names = {name.casefold() for name in normalized_names}
        if not folded_names:
            return set()
        result = await session.execute(
            select(Tag.id, Tag.normalized_name).where(
                Tag.normalized_name.in_(folded_names)
            )
        )
        matches: dict[str, str] = {}
        for tag_id, normalized_name in result.all():
            if not isinstance(normalized_name, str):
                continue
            folded = normalized_name.casefold()
            if folded in matches and matches[folded] != tag_id:
                raise ValueError("Markdown 标签同步冲突")
            matches[folded] = tag_id
        for name in normalized_names:
            folded = name.casefold()
            if folded in matches:
                continue
            tag = Tag(name=name, normalized_name=folded)
            try:
                async with session.begin_nested():
                    session.add(tag)
                    await session.flush()
            except IntegrityError:
                existing = await session.execute(
                    select(Tag.id, Tag.normalized_name).where(
                        Tag.normalized_name == folded
                    )
                )
                row = existing.first()
                if row is None or not isinstance(row.normalized_name, str):
                    raise ValueError("Markdown 标签同步冲突")
                matches[row.normalized_name.casefold()] = row.id
            else:
                matches[folded] = tag.id
        return set(matches.values())

    @staticmethod
    async def _replace_tag_relations(
        session: AsyncSession, item_id: str, desired_ids: set[str]
    ) -> None:
        result = await session.execute(
            select(KnowledgeItemTag).where(
                KnowledgeItemTag.knowledge_item_id == item_id
            )
        )
        existing = list(result.scalars())
        existing_ids = {relation.tag_id for relation in existing}
        for relation in existing:
            if relation.tag_id not in desired_ids:
                await session.delete(relation)
        for tag_id in desired_ids - existing_ids:
            session.add(KnowledgeItemTag(tag_id=tag_id, knowledge_item_id=item_id))

    async def stage_collection_note(
        self,
        session: AsyncSession,
        item: KnowledgeItem,
        collection_names: Sequence[str],
        *,
        skip_if_unchanged: bool = False,
    ) -> tuple[ObsidianVault, StagedWrite | None, bytes, str]:
        """Stage a collection-only note rewrite without creating a content version."""

        if (
            item.status != "published"
            or not item.current_content_version_id
        ):
            raise ApplicationError(409, "collection_item_invalid", "条目当前不可加入合集")
        binding_result = await session.execute(
            select(NoteBinding).where(NoteBinding.knowledge_item_id == item.id)
        )
        binding = binding_result.scalar_one_or_none()
        if binding is None:
            raise ApplicationError(409, "note_binding_missing", "发布条目缺少笔记绑定")
        current = await session.get(ContentVersion, item.current_content_version_id)
        if current is None or current.knowledge_item_id != item.id:
            raise ApplicationError(409, "content_version_invalid", "当前内容版本无效")
        vault = self.vault()
        try:
            disk_note = vault.read(binding.relative_path)
            old_raw = vault.read_bytes(binding.relative_path)
        except (OSError, ValueError) as error:
            raise ApplicationError(
                409,
                "collection_note_unavailable",
                "受管理 Markdown 当前不可更新",
            ) from error
        disk_hash = content_hash(disk_note.body)
        if disk_hash != binding.content_hash:
            raise ApplicationError(
                409,
                "content_conflict",
                "Obsidian 内容已变化，请重新扫描后再修改合集",
            )
        try:
            tags = self._frontmatter_tags(disk_note.metadata)
            disk_collections = normalize_collection_names(
                disk_note.metadata.get("collections", [])
            )
            desired_collections = normalize_collection_names(list(collection_names))
            source_url = self._frontmatter_source_url(
                disk_note.metadata,
                self._source_url(current.source_metadata_json),
            )
        except ValueError as error:
            raise ApplicationError(
                409,
                "collection_note_unavailable",
                "受管理 Markdown Frontmatter 无效",
            ) from error
        if (
            skip_if_unchanged
            and len(disk_collections) == len(desired_collections)
            and {name.casefold() for name in disk_collections}
            == {name.casefold() for name in desired_collections}
        ):
            return vault, None, old_raw, binding.relative_path
        title = (
            disk_note.metadata["title"]
            if isinstance(disk_note.metadata.get("title"), str)
            else current.title
        )
        raw = render_note(
            zhiliu_id=binding.zhiliu_id,
            source_type=item.source_type,
            title=title,
            body=disk_note.body,
            status="reviewed",
            created_at=item.created_at,
            updated_at=datetime.now(timezone.utc),
            tags=tags,
            collections=desired_collections,
            source_url=source_url,
        )
        try:
            staged = vault.stage_write(binding.relative_path, raw)
        except (OSError, ValueError) as error:
            raise ApplicationError(
                409,
                "collection_note_unavailable",
                "受管理 Markdown 当前不可更新",
            ) from error
        return vault, staged, old_raw, binding.relative_path

    async def _collection_ids_for_names(
        self, session: AsyncSession, collection_names: Sequence[str]
    ) -> set[str]:
        normalized_names = normalize_collection_names(list(collection_names))
        folded_names = {name.casefold() for name in normalized_names}
        if not folded_names:
            return set()
        result = await session.execute(
            select(Collection.id, Collection.name).where(
                func.lower(Collection.name).in_(folded_names)
            )
        )
        matches: dict[str, str] = {}
        for collection_id, name in result.all():
            if not isinstance(name, str):
                continue
            folded = name.casefold()
            if folded in matches and matches[folded] != collection_id:
                raise ValueError("Markdown 合集同步冲突")
            matches[folded] = collection_id
        for name in normalized_names:
            folded = name.casefold()
            if folded in matches:
                continue
            collection = Collection(name=name, description=None)
            try:
                async with session.begin_nested():
                    session.add(collection)
                    await session.flush()
            except IntegrityError:
                existing = await session.execute(
                    select(Collection.id, Collection.name).where(
                        func.lower(Collection.name) == folded
                    )
                )
                row = existing.first()
                if row is None or not isinstance(row.name, str):
                    raise ValueError("Markdown 合集同步冲突")
                matches[row.name.casefold()] = row.id
            else:
                matches[folded] = collection.id
        return set(matches.values())

    @staticmethod
    async def _replace_collection_relations(
        session: AsyncSession, item_id: str, desired_ids: set[str]
    ) -> None:
        result = await session.execute(
            select(CollectionItem).where(CollectionItem.knowledge_item_id == item_id)
        )
        existing = list(result.scalars())
        existing_ids = {relation.collection_id for relation in existing}
        for relation in existing:
            if relation.collection_id not in desired_ids:
                await session.delete(relation)
        for collection_id in desired_ids - existing_ids:
            session.add(
                CollectionItem(
                    collection_id=collection_id,
                    knowledge_item_id=item_id,
                )
            )

    @staticmethod
    def _source_url(source_metadata_json: str) -> str | None:
        try:
            metadata = json.loads(source_metadata_json or "{}")
        except json.JSONDecodeError:
            return None
        value = metadata.get("url") if isinstance(metadata, dict) else None
        return value if isinstance(value, str) else None

    @staticmethod
    def _frontmatter_tags(metadata: dict[str, object]) -> list[str]:
        try:
            return normalize_tag_names(metadata.get("tags", []))
        except ValueError as error:
            raise ValueError("Frontmatter 标签无效") from error

    @staticmethod
    def _frontmatter_collections(metadata: dict[str, object]) -> list[str]:
        try:
            return normalize_collection_names(metadata.get("collections", []))
        except ValueError as error:
            raise ValueError("Frontmatter 合集无效") from error

    @classmethod
    def _frontmatter_relation_names(
        cls, metadata: dict[str, object]
    ) -> tuple[list[str], list[str]]:
        """Validate the two independent, user-editable Frontmatter lists."""

        try:
            collection_names = cls._frontmatter_collections(metadata)
            tag_names = cls._frontmatter_tags(metadata)
        except ValueError as error:
            raise ValueError("Frontmatter 关系无效") from error
        return collection_names, tag_names

    @staticmethod
    def _frontmatter_source_url(
        metadata: dict[str, object], fallback: str | None
    ) -> str | None:
        value = metadata.get("source_url", fallback)
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or len(value) > 2048
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or redact_sensitive_text(value) != value
        ):
            raise ValueError("Frontmatter 来源无效")
        return value or None

    @staticmethod
    def _vault_source_metadata(
        item: KnowledgeItem,
        body: str,
        relative_path: str,
        *,
        source_url: str | None = None,
    ) -> str:
        normalized_body = normalize_content(body).rstrip("\n")
        metadata: dict[str, object] = {
            "source_type": item.source_type,
            "media_type": "text/markdown",
            "segments": [
                {
                    "text": normalized_body,
                    "locator": {"kind": "obsidian", "path": relative_path},
                }
            ],
        }
        if source_url:
            metadata["url"] = source_url
        return json.dumps(metadata, ensure_ascii=False)

    async def _apply_vault_version(
        self,
        session: AsyncSession,
        item: KnowledgeItem,
        binding: NoteBinding,
        title: str,
        body: str,
        relative_path: str,
        source_metadata_json: str | None = None,
    ) -> ContentVersion:
        if self.embedding_provider is None:
            raise ApplicationError(409, "embedding_not_configured", "Embedding capability 未配置")
        source_version = (
            await session.get(ContentVersion, item.current_content_version_id)
            if item.current_content_version_id
            else None
        )
        version = await self._new_vault_version(
            session,
            item,
            title,
            body,
            relative_path,
            source_metadata_json=source_metadata_json,
            summary=source_version.summary if source_version is not None else None,
            suggested_tags_json=(
                source_version.suggested_tags_json
                if source_version is not None
                else "[]"
            ),
            suggested_collections_json=(
                source_version.suggested_collections_json
                if source_version is not None
                else "[]"
            ),
            prompt_version=source_version.prompt_version if source_version is not None else None,
        )
        index = IndexService(self.vector_store, self.embedding_provider)
        await index.index_version(session, item, version, relative_path)
        digest = content_hash(body)
        self._activate_vault_version(item, binding, version, relative_path, digest)
        return version

    async def _new_vault_version(
        self,
        session: AsyncSession,
        item: KnowledgeItem,
        title: str,
        body: str,
        relative_path: str,
        *,
        source_metadata_json: str | None = None,
        summary: str | None = None,
        suggested_tags_json: str = "[]",
        suggested_collections_json: str = "[]",
        prompt_version: str | None = None,
    ) -> ContentVersion:
        version = ContentVersion(
            knowledge_item_id=item.id,
            version_no=await self._next_version_no(session, item.id),
            source_kind="vault",
            title=title[:300],
            body=normalize_content(body),
            content_hash=content_hash(body),
            summary=summary,
            suggested_tags_json=suggested_tags_json,
            suggested_collections_json=suggested_collections_json,
            prompt_version=prompt_version,
            source_metadata_json=source_metadata_json
            or self._vault_source_metadata(item, body, relative_path),
        )
        session.add(version)
        await session.flush()
        return version

    @staticmethod
    def _activate_vault_version(
        item: KnowledgeItem,
        binding: NoteBinding,
        version: ContentVersion,
        relative_path: str,
        digest: str,
    ) -> None:
        item.title = version.title
        item.content_hash = digest
        item.current_content_version_id = version.id
        item.status = "published"
        item.updated_at = datetime.now(timezone.utc)
        binding.relative_path = relative_path
        binding.content_hash = digest
        binding.last_written_hash = digest
        binding.sync_state = "synced"
        binding.last_synced_at = datetime.now(timezone.utc)
        binding.last_error = None

    def _discard_candidate_vectors(self, version_id: str) -> None:
        for attempt in range(2):
            try:
                self.vector_store.delete_version(version_id)
                return
            except Exception as error:
                if attempt == 1:
                    logger.error(
                        "publish_candidate_vector_cleanup_failed",
                        error_type=type(error).__name__,
                    )

    async def rescan(self, minimum_file_age_seconds: float = 0) -> dict[str, int]:
        async with self.mutation_lock:
            return await self._rescan(minimum_file_age_seconds)

    async def _rescan(self, minimum_file_age_seconds: float) -> dict[str, int]:
        vault = self.vault()
        found: dict[str, list[tuple[str, object, bool]]] = {}
        present_relative_paths: set[str] = set()
        invalid = 0
        for path in vault.iter_markdown():
            relative = vault.relative_path(path)
            present_relative_paths.add(relative)
            try:
                before = path.stat()
                note = parse_note(path.read_text(encoding="utf-8"))
                after = path.stat()
            except (OSError, ValueError):
                invalid += 1
                continue
            if note.zhiliu_id:
                stable = (
                    before.st_mtime_ns == after.st_mtime_ns
                    and before.st_size == after.st_size
                    and (
                        minimum_file_age_seconds <= 0
                        or time.time() - after.st_mtime >= minimum_file_age_seconds
                    )
                )
                found.setdefault(note.zhiliu_id, []).append((relative, note, stable))
        changed = renamed = missing = conflicts = deferred = 0
        reconcile_versions: dict[str, str] = {}
        initial_reconciliation = not self._initial_vector_reconciliation_complete
        async with self.session_factory() as session, session.begin():
            bindings = list((await session.execute(select(NoteBinding))).scalars())
            bindings_by_id = {binding.zhiliu_id: binding for binding in bindings}
            for zhiliu_id, notes in found.items():
                binding = bindings_by_id.get(zhiliu_id)
                item = await session.get(KnowledgeItem, zhiliu_id)
                if len(notes) > 1:
                    conflicts += 1
                    if binding:
                        binding.sync_state = "conflict"
                        binding.last_error = "同一 zhiliu_id 出现在多个文件"
                    continue
                relative, note_object, stable = notes[0]
                note = note_object
                if item is None:
                    continue
                try:
                    collection_names, tag_names = self._frontmatter_relation_names(
                        note.metadata
                    )
                    if (
                        item.deleted_at is None
                        and item.status == "published"
                        and item.current_content_version_id
                    ):
                        current = await session.get(
                            ContentVersion, item.current_content_version_id
                        )
                        if current is None or current.knowledge_item_id != item.id:
                            raise ValueError("Markdown 合集引用无效")
                        if "collections" not in note.metadata:
                            collection_names = await self._collection_names(
                                session, item.id
                            )
                        collection_ids = await self._collection_ids_for_names(
                            session, collection_names
                        )
                        tag_ids = await self._tag_ids_for_names(session, tag_names)
                    elif collection_names or tag_names:
                        raise ValueError("Markdown Frontmatter 关系无效")
                    else:
                        collection_ids = set()
                        tag_ids = set()
                except ValueError:
                    invalid += 1
                    if binding:
                        binding.sync_state = "error"
                        binding.last_error = "Markdown Frontmatter 关系无效"
                    continue
                if binding is None:
                    binding = NoteBinding(
                        knowledge_item_id=item.id,
                        zhiliu_id=item.id,
                        relative_path=relative,
                        content_hash=content_hash(note.body),
                        sync_state="changed",
                    )
                    session.add(binding)
                    await session.flush()
                    bindings_by_id[zhiliu_id] = binding
                if binding.relative_path != relative:
                    binding.relative_path = relative
                    renamed += 1
                digest = content_hash(note.body)
                if digest != binding.content_hash:
                    if not stable:
                        binding.sync_state = "changed"
                        binding.last_error = None
                        deferred += 1
                        continue
                    title_value = note.metadata.get("title")
                    title = title_value if isinstance(title_value, str) else item.title
                    await self._apply_vault_version(
                        session, item, binding, title, note.body, relative
                    )
                    changed += 1
                    if item.current_content_version_id:
                        reconcile_versions[item.id] = item.current_content_version_id
                else:
                    binding.sync_state = "synced"
                    binding.last_synced_at = datetime.now(timezone.utc)
                if (
                    item.deleted_at is None
                    and item.status == "published"
                    and item.current_content_version_id
                ):
                    await self._replace_collection_relations(
                        session, item.id, collection_ids
                    )
                    await self._replace_tag_relations(session, item.id, tag_ids)
            for binding in bindings:
                if binding.zhiliu_id not in found:
                    if binding.relative_path in present_relative_paths:
                        binding.sync_state = "error"
                        binding.last_error = "Markdown 暂时无法解析，等待稳定后重试"
                        continue
                    binding.sync_state = "missing"
                    binding.last_error = "受管理 Markdown 文件不存在"
                    missing += 1
            if initial_reconciliation:
                published = (
                    await session.execute(
                        select(KnowledgeItem).where(
                            KnowledgeItem.status == "published",
                            KnowledgeItem.current_content_version_id.is_not(None),
                        )
                    )
                ).scalars()
                for item in published:
                    if item.current_content_version_id:
                        reconcile_versions[item.id] = item.current_content_version_id
        for item_id, version_id in reconcile_versions.items():
            self.vector_store.delete_item_except_version(item_id, version_id)
        if initial_reconciliation:
            self._initial_vector_reconciliation_complete = True
        return {
            "changed": changed,
            "renamed": renamed,
            "missing": missing,
            "conflicts": conflicts,
            "invalid": invalid,
            "deferred": deferred,
        }

    async def get_item(
        self, item_id: str
    ) -> tuple[KnowledgeItem, ContentVersion | None, NoteBinding | None]:
        async with self.session_factory() as session:
            item = await self._item(session, item_id)
            version = (
                await self._version(session, item)
                if item.pending_content_version_id or item.current_content_version_id
                else None
            )
            binding_result = await session.execute(
                select(NoteBinding).where(NoteBinding.knowledge_item_id == item.id)
            )
            binding = binding_result.scalar_one_or_none()
            if (
                item.status == "published"
                and binding is not None
                and item.pending_content_version_id is None
            ):
                try:
                    note = self.vault().read(binding.relative_path)
                except (OSError, ValueError):
                    # Editors can briefly expose an incomplete file while saving.
                    # Keep serving the last successfully indexed version until a
                    # later rescan observes a stable, valid Markdown document.
                    pass
                else:
                    try:
                        self._frontmatter_relation_names(note.metadata)
                    except ValueError:
                        # An invalid user-edited Frontmatter must not make an
                        # unvalidated disk body appear as the published
                        # current version.  Rescan records the binding error;
                        # reads continue to use the last indexed version.
                        pass
                    else:
                        if version is not None:
                            version.body = note.body
                            version.content_hash = content_hash(note.body)
            return item, version, binding

    async def soft_delete(self, item_id: str) -> None:
        async with self.mutation_lock:
            async with self.session_factory() as session, session.begin():
                item = await self._item(session, item_id)
                item.status = "deleted"
                item.deleted_at = datetime.now(timezone.utc)
                item.updated_at = item.deleted_at
            self.vector_store.delete_item(item_id)

    async def _item(self, session: AsyncSession, item_id: str) -> KnowledgeItem:
        item = await session.get(KnowledgeItem, item_id)
        if item is None or item.deleted_at is not None:
            raise ApplicationError(404, "item_not_found", "知识条目不存在")
        return item

    async def _version(self, session: AsyncSession, item: KnowledgeItem) -> ContentVersion:
        version_id = item.pending_content_version_id or item.current_content_version_id
        if not version_id:
            raise ApplicationError(409, "content_not_ready", "内容版本尚未生成")
        version = await session.get(ContentVersion, version_id)
        if version is None or version.knowledge_item_id != item.id:
            raise ApplicationError(500, "content_version_invalid", "当前内容版本无效")
        return version
