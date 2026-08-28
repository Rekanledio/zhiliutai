"""Stage 5 video source, subtitle, ASR and visual processing service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import re
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.db.models import ContentVersion, KnowledgeItem, ProcessingJob, SourceArtifact
from app.ingestion.fetcher import UnsafeUrlError, validate_public_url
from app.providers.video import (
    ASRProvider,
    AudioExtractionOptions,
    AudioExtractor,
    OCRProvider,
    SceneDetector,
    VideoCapabilityError,
    VideoDownloadOptions,
    VideoDownloadResult,
    VideoProviderError,
    VideoSecurityError,
    VideoSourceProvider,
    VisionProvider,
)
from app.services.artifacts import ArtifactStore, StoredArtifact
from app.services.content import content_hash, normalize_content
from app.video.notes import (
    VideoTextError,
    build_chapters,
    render_layered_note,
    validate_transcript_segments,
    validate_video_alignment,
)
from app.video.subtitles import (
    SubtitleParseError,
    normalize_subtitle_track,
    render_transcript_vtt,
)
from app.video.types import (
    Keyframe,
    ProcessingManifest,
    SubtitleTrack,
    TranscriptSegment,
    VideoSourceMetadata,
    VisualEvent,
)


class VideoJobCancelled(RuntimeError):
    """The persisted job was cancelled while a provider operation was running."""


class VideoProcessingError(RuntimeError):
    """Internal video failure with a safe public message."""


UrlValidator = Callable[[str], None]


class VideoService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        artifacts: ArtifactStore,
        *,
        video_provider: VideoSourceProvider,
        asr_provider: ASRProvider | None = None,
        audio_extractor: AudioExtractor | None = None,
        scene_detector: SceneDetector | None = None,
        vision_provider: VisionProvider | None = None,
        ocr_provider: OCRProvider | None = None,
        url_validator: UrlValidator = validate_public_url,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.artifacts = artifacts
        self.video_provider = video_provider
        self.asr_provider = asr_provider
        self.audio_extractor = audio_extractor
        self.scene_detector = scene_detector
        self.vision_provider = vision_provider
        self.ocr_provider = ocr_provider
        self.url_validator = url_validator

    def _validate_url(self, url: str) -> None:
        try:
            self.url_validator(url)
        except UnsafeUrlError as error:
            raise ApplicationError(422, "unsafe_url", str(error)) from error

    async def submit_video(
        self,
        url: str,
        title: str | None,
        language: str | None,
        idempotency_key: str | None,
        enable_vision: bool,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        self._validate_url(url)
        stored_url = self.artifacts.put_text(url, ".url")
        async with self.session_factory() as session, session.begin():
            if idempotency_key:
                existing_result = await session.execute(
                    select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
                )
                existing_job = existing_result.scalar_one_or_none()
                if existing_job is not None:
                    payload = self._payload(existing_job)
                    if payload.get("source_url_hash") != stored_url.content_hash:
                        raise ApplicationError(409, "idempotency_conflict", "同一幂等键已用于不同视频来源")
                    existing_item = await session.get(KnowledgeItem, payload.get("item_id"))
                    if existing_item is None:
                        raise ApplicationError(409, "idempotency_conflict", "幂等任务关联记录不存在")
                    return existing_item, existing_job, True

            duplicate_result = await session.execute(
                select(KnowledgeItem)
                .join(SourceArtifact, SourceArtifact.knowledge_item_id == KnowledgeItem.id)
                .where(
                    SourceArtifact.artifact_type == "video_source",
                    SourceArtifact.content_hash == stored_url.content_hash,
                    KnowledgeItem.deleted_at.is_(None),
                )
                .order_by(KnowledgeItem.created_at)
                .limit(1)
            )
            duplicate = duplicate_result.scalar_one_or_none()
            if duplicate is not None:
                job_result = await session.execute(
                    select(ProcessingJob)
                    .where(ProcessingJob.payload_json.like(f'%%"item_id": "{duplicate.id}"%%'))
                    .order_by(ProcessingJob.created_at.desc())
                    .limit(1)
                )
                existing_job = job_result.scalar_one_or_none()
                if existing_job is None:
                    existing_job = ProcessingJob(
                        kind="ingest_video",
                        state="succeeded",
                        stage="deduplicated",
                        progress=1.0,
                        payload_json=json.dumps({"item_id": duplicate.id}, ensure_ascii=False),
                        result_json=json.dumps({"item_id": duplicate.id}, ensure_ascii=False),
                    )
                    session.add(existing_job)
                return duplicate, existing_job, True

            item = KnowledgeItem(
                title=(title or self._fallback_title(url))[:300],
                source_type="video",
                status="processing",
                content_hash=stored_url.content_hash,
            )
            session.add(item)
            await session.flush()
            source_artifact = SourceArtifact(
                knowledge_item_id=item.id,
                artifact_type="video_source",
                media_type="text/uri-list",
                relative_path=stored_url.relative_path,
                content_hash=stored_url.content_hash,
                byte_size=stored_url.byte_size,
                source_locator=json.dumps(
                    {"kind": "video_url", "requested_url": url},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                metadata_json=json.dumps(
                    {
                        "requested_url": url,
                        "source_content_hash": stored_url.content_hash,
                        "language": language,
                        "enable_vision": enable_vision,
                        "title_provided": title is not None,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                retention_policy="permanent",
            )
            session.add(source_artifact)
            await session.flush()
            payload: dict[str, object] = {
                "item_id": item.id,
                "artifact_id": source_artifact.id,
                "source_type": "video",
                "source_url": url,
                "source_url_hash": stored_url.content_hash,
                "title_provided": title is not None,
                "language": language,
                "enable_vision": enable_vision,
            }
            job = ProcessingJob(
                kind="ingest_video",
                payload_json=json.dumps(payload, ensure_ascii=False),
                idempotency_key=idempotency_key,
            )
            session.add(job)
            await session.flush()
            return item, job, False

    async def reprocess(self, item_id: str) -> tuple[KnowledgeItem, ProcessingJob]:
        async with self.session_factory() as session, session.begin():
            item = await session.get(KnowledgeItem, item_id)
            if item is None or item.deleted_at is not None or item.source_type != "video":
                raise ApplicationError(404, "item_not_found", "视频知识条目不存在")
            artifact_result = await session.execute(
                select(SourceArtifact)
                .where(
                    SourceArtifact.knowledge_item_id == item.id,
                    SourceArtifact.artifact_type == "video_source",
                )
                .order_by(SourceArtifact.created_at)
                .limit(1)
            )
            artifact = artifact_result.scalar_one_or_none()
            locator = self._json_object(artifact.source_locator if artifact else None)
            url = locator.get("requested_url") if locator else None
            if artifact is None or not isinstance(url, str):
                raise ApplicationError(409, "source_url_missing", "视频来源 URL 不存在")
            self._validate_url(url)
            source_metadata = self._json_object(artifact.metadata_json) or {}
            language = source_metadata.get("language")
            enable_vision = source_metadata.get("enable_vision")
            title_provided = source_metadata.get("title_provided")
            payload = {
                "item_id": item.id,
                "artifact_id": artifact.id,
                "source_type": "video",
                "source_url": url,
                "source_url_hash": artifact.content_hash,
                "title_provided": title_provided if isinstance(title_provided, bool) else True,
                "language": language if isinstance(language, str) else None,
                "enable_vision": enable_vision if isinstance(enable_vision, bool) else False,
            }
            job = ProcessingJob(
                kind="ingest_video",
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
            session.add(job)
            await session.flush()
            return item, job

    async def process_video(self, job: ProcessingJob) -> dict[str, object]:
        payload = self._payload(job)
        item_id = payload.get("item_id")
        artifact_id = payload.get("artifact_id")
        url = payload.get("source_url")
        if not all(isinstance(value, str) and value for value in (item_id, artifact_id, url)):
            raise VideoProcessingError("视频采集任务记录不完整")
        self._validate_url(url)
        async with self.session_factory() as session:
            item = await session.get(KnowledgeItem, item_id)
            source_artifact = await session.get(SourceArtifact, artifact_id)
        if (
            item is None
            or source_artifact is None
            or item.source_type != "video"
            or source_artifact.artifact_type != "video_source"
            or source_artifact.knowledge_item_id != item.id
            or not self.artifacts.verify(
                source_artifact.relative_path,
                source_artifact.content_hash,
                source_artifact.byte_size,
            )
        ):
            raise VideoProcessingError("视频采集任务记录不完整")

        staged: list[StoredArtifact] = []
        try:
            async with self._temporary_directory() as workdir:
                await self._job_stage(job.id, "source_metadata", 0.1)
                options = VideoDownloadOptions(
                    max_bytes=self.settings.video_max_bytes,
                    max_duration_ms=self.settings.video_max_duration_seconds * 1000,
                    timeout_seconds=self.settings.video_fetch_timeout,
                    max_redirects=self.settings.video_max_redirects,
                    subtitle_languages=self._language_tuple(payload.get("language")),
                    max_subtitle_bytes=self.settings.video_max_subtitle_bytes,
                )
                acquisition = await self.video_provider.acquire(
                    url,
                    destination=workdir,
                    options=options,
                )
                self._validate_redirect_chain(
                    acquisition.redirect_chain,
                    url,
                    options.max_redirects,
                    network_policy_enforced=acquisition.network_policy_enforced,
                )
                await self._check_cancelled(job.id)
                metadata = self._normalize_metadata(acquisition, url, options)
                self._validate_redirect_chain(
                    acquisition.redirect_chain,
                    url,
                    options.max_redirects,
                    final_url=metadata.final_url,
                    network_policy_enforced=acquisition.network_policy_enforced,
                )
                if len(acquisition.subtitle_tracks) > 64:
                    raise VideoProviderError("字幕轨道数量超过限制")

                media_stored: StoredArtifact | None = None
                if acquisition.media_path is not None:
                    media_path = self._provider_path(acquisition.media_path, workdir)
                    media_stored = self.artifacts.put_file(
                        media_path, ".media", max_bytes=self.settings.video_max_bytes
                    )
                    staged.append(media_stored)
                    metadata = metadata.model_copy(
                        update={"source_content_hash": media_stored.content_hash}
                    )
                else:
                    metadata = metadata.model_copy(
                        update={"source_content_hash": source_artifact.content_hash}
                    )

                selected = self._select_subtitle(
                    acquisition.subtitle_tracks,
                    payload.get("language"),
                )
                if selected is not None:
                    await self._job_stage(job.id, "subtitle_normalization", 0.35)
                    self._validate_subtitle_source(selected)
                    segments = normalize_subtitle_track(
                        selected,
                        duration_ms=metadata.duration_ms,
                        max_bytes=self.settings.video_max_subtitle_bytes,
                        max_segments=self.settings.video_max_subtitle_segments,
                    )
                    raw_suffix = "." + (selected.format if selected.format in {"vtt", "srt", "json3"} else "bin")
                    subtitle_stored = self.artifacts.put_bytes(selected.content, raw_suffix)
                    transcript_stored = self.artifacts.put_bytes(
                        render_transcript_vtt(segments), ".vtt"
                    )
                    staged.extend((subtitle_stored, transcript_stored))
                    processing_status = "subtitle_ready"
                    source_kind = "video_subtitle"
                    asr_model = None
                else:
                    segments, processing_status, source_kind, asr_model = await self._asr_fallback(
                        job.id,
                        metadata,
                        media_stored,
                        workdir,
                        payload.get("language"),
                    )
                    if processing_status == "asr_required":
                        await self._cleanup_staged(staged)
                        return {
                            "item_id": item.id,
                            "source_type": "video",
                            "status": "asr_required",
                            "asr_called": False,
                            "_job_stage": "asr_required",
                            "_job_progress": 0.6,
                        }
                    transcript_stored = self.artifacts.put_bytes(
                        render_transcript_vtt(segments), ".vtt"
                    )
                    staged.append(transcript_stored)
                    subtitle_stored = None

                await self._check_cancelled(job.id)
                (
                    visual_events,
                    keyframes,
                    keyframe_artifacts,
                    vision_model,
                    ocr_model,
                ) = await self._visual_processing(
                    job.id,
                    metadata,
                    media_stored,
                    workdir,
                    bool(payload.get("enable_vision")),
                )
                validate_video_alignment(
                    segments,
                    visual_events,
                    duration_ms=metadata.duration_ms,
                )
                staged.extend(keyframe_artifacts)
                chapters = build_chapters(segments)
                body, metadata_segments = render_layered_note(
                    segments,
                    chapters,
                    visual_events,
                    transcript_artifact_id=transcript_stored.content_hash,
                )
                manifest = ProcessingManifest(
                    source_metadata=metadata,
                    transcript_segments=segments,
                    chapters=chapters,
                    keyframes=keyframes,
                    visual_events=visual_events,
                    source_provider=metadata.provider,
                    source_tool_version=metadata.tool_version,
                    source_content_hash=media_stored.content_hash if media_stored else source_artifact.content_hash,
                    transcript_artifact_id=None,
                    media_artifact_id=None,
                    subtitle_artifact_ids=[],
                    asr_provider=getattr(self.asr_provider, "provider", None)
                    if source_kind == "video_asr"
                    else None,
                    asr_model=asr_model,
                    vision_provider=getattr(self.vision_provider, "provider", None)
                    if visual_events
                    else None,
                    vision_model=vision_model,
                    ocr_provider=getattr(self.ocr_provider, "provider", None)
                    if ocr_model
                    else None,
                    ocr_model=ocr_model,
                    processing_status=processing_status,
                )
                result = await self._persist_video_result(
                    item.id,
                    source_artifact.id,
                    body,
                    metadata,
                    metadata_segments,
                    manifest,
                    media_stored=media_stored,
                    subtitle_stored=subtitle_stored,
                    transcript_stored=transcript_stored,
                    keyframe_artifacts=keyframe_artifacts,
                    title_provided=bool(payload.get("title_provided")),
                    source_kind=source_kind,
                    source_url_hash=source_artifact.content_hash,
                    subtitle_track=selected,
                    job_id=job.id,
                )
                if media_stored and self.settings.video_media_retention_policy == "delete_after_processing":
                    cleaned = await self._cleanup_artifact(media_stored, immediate=True)
                    result["media_cleanup"] = "deleted" if cleaned else "failed"
                elif media_stored:
                    result["media_cleanup"] = self.settings.video_media_retention_policy
                result["_job_stage"] = "pending_review"
                result["_job_progress"] = 1.0
                return result
        except (VideoJobCancelled, asyncio.CancelledError):
            await self._cleanup_staged(staged)
            raise
        except Exception:
            await self._cleanup_staged(staged)
            await self._mark_failed_item(item.id)
            raise

    @staticmethod
    def _payload(job: ProcessingJob) -> dict[str, object]:
        try:
            value = json.loads(job.payload_json or "{}")
        except json.JSONDecodeError as error:
            raise VideoProcessingError("视频采集任务参数无效") from error
        if not isinstance(value, dict):
            raise VideoProcessingError("视频采集任务参数无效")
        return value

    @staticmethod
    def _fallback_title(url: str) -> str:
        from urllib.parse import urlsplit

        return urlsplit(url).hostname or "视频来源"

    @staticmethod
    def _json_object(value: str | None) -> dict[str, object] | None:
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _language_tuple(value: object) -> tuple[str, ...]:
        if not isinstance(value, str) or not value.strip():
            return ()
        return (value.strip()[:32],)

    def _normalize_metadata(
        self,
        acquisition: VideoDownloadResult,
        requested_url: str,
        options: VideoDownloadOptions,
    ) -> VideoSourceMetadata:
        metadata = acquisition.metadata
        final_url = metadata.final_url or metadata.source_url or requested_url
        self._validate_url(final_url)
        if metadata.requested_url:
            self._validate_url(metadata.requested_url)
        if metadata.duration_ms is not None and metadata.duration_ms > options.max_duration_ms:
            raise VideoProviderError("视频时长超过限制")
        provider = metadata.provider or getattr(self.video_provider, "name", "video-provider")
        tool_version = metadata.tool_version or getattr(
            self.video_provider, "tool_version", "unknown"
        )
        return metadata.model_copy(
            update={
                "source_url": final_url,
                "requested_url": requested_url,
                "final_url": final_url,
                "provider": provider[:200],
                "tool_version": tool_version[:200],
            }
        )

    def _validate_redirect_chain(
        self,
        chain: Sequence[str],
        requested_url: str,
        max_redirects: int,
        *,
        final_url: str | None = None,
        network_policy_enforced: bool = False,
    ) -> None:
        if not chain:
            if (
                final_url is not None
                and final_url != requested_url
                and not network_policy_enforced
            ):
                raise VideoSecurityError("视频来源缺少可验证的重定向链")
            return
        if len(chain) > max_redirects + 1 or chain[0] != requested_url:
            raise VideoSecurityError("视频来源重定向次数超过限制")
        for hop in chain:
            if not isinstance(hop, str) or not hop:
                raise VideoSecurityError("视频来源重定向地址无效")
            self._validate_url(hop)
        if final_url is not None and chain[-1] != final_url:
            raise VideoSecurityError("视频来源重定向链与最终 URL 不一致")

    def _provider_path(self, path: Path, workdir: Path) -> Path:
        resolved_workdir = workdir.resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_workdir) or not resolved.is_file():
            raise VideoSecurityError("视频 provider 输出路径无效")
        return resolved

    def _select_subtitle(
        self,
        tracks: Sequence[SubtitleTrack],
        requested_language: object,
    ) -> SubtitleTrack | None:
        if not tracks:
            return None
        requested = requested_language.casefold().strip() if isinstance(requested_language, str) else ""

        def score(track: SubtitleTrack) -> tuple[int, int, str]:
            language = (track.language or "").casefold()
            exact = int(bool(requested and language == requested))
            base = int(bool(requested and language.split("-", 1)[0] == requested.split("-", 1)[0]))
            return (exact * 4 + base * 2, int(not track.is_automatic), language)

        return max(tracks, key=score)

    def _validate_subtitle_source(self, track: SubtitleTrack) -> None:
        if len(track.content) > self.settings.video_max_subtitle_bytes:
            raise SubtitleParseError("字幕文件超过大小限制")
        if track.source_url:
            self._validate_url(track.source_url)

    async def _asr_fallback(
        self,
        job_id: str,
        metadata: VideoSourceMetadata,
        media_stored: StoredArtifact | None,
        workdir: Path,
        language: object,
    ) -> tuple[list[TranscriptSegment], str, str, str | None]:
        if (
            not self.settings.video_asr_fallback_enabled
            or media_stored is None
            or self.asr_provider is None
            or self.audio_extractor is None
        ):
            return [], "asr_required", "video_asr", None
        await self._job_stage(job_id, "audio_extraction", 0.45)
        media_path = self.artifacts.root / media_stored.relative_path
        try:
            audio_path = await self.audio_extractor.extract(
                media_path,
                destination=workdir,
                options=AudioExtractionOptions(
                    max_bytes=self.settings.video_max_audio_bytes,
                    timeout_seconds=self.settings.video_fetch_timeout,
                ),
            )
        except VideoCapabilityError:
            return [], "asr_required", "video_asr", None
        audio_path = self._provider_path(audio_path, workdir)
        if audio_path.stat().st_size > self.settings.video_max_audio_bytes:
            raise VideoProviderError("音轨文件超过大小限制")
        await self._job_stage(job_id, "asr", 0.65)
        segments = list(
            await self.asr_provider.transcribe(
                audio_path.read_bytes(),
                language=language if isinstance(language, str) else None,
                duration_ms=metadata.duration_ms,
            )
        )
        normalized = validate_transcript_segments(
            segments,
            duration_ms=metadata.duration_ms,
            max_segments=self.settings.video_max_subtitle_segments,
        )
        if not normalized:
            raise VideoProcessingError("转录结果为空")
        return normalized, "asr_complete", "video_asr", getattr(
            self.asr_provider, "model", None
        )

    async def _visual_processing(
        self,
        job_id: str,
        metadata: VideoSourceMetadata,
        media_stored: StoredArtifact | None,
        workdir: Path,
        enabled: bool,
    ) -> tuple[
        list[VisualEvent],
        list[Keyframe],
        list[StoredArtifact],
        str | None,
        str | None,
    ]:
        if (
            not enabled
            or metadata.video_kind not in {"slideshow", "tutorial"}
            or media_stored is None
            or self.scene_detector is None
            or (self.vision_provider is None and self.ocr_provider is None)
        ):
            return [], [], [], None, None
        await self._job_stage(job_id, "scene_detection", 0.75)
        samples = list(
            await self.scene_detector.detect(
                self.artifacts.root / media_stored.relative_path,
                metadata=metadata,
                destination=workdir,
                max_keyframes=self.settings.video_max_keyframes,
            )
        )
        if len(samples) > self.settings.video_max_keyframes:
            raise VideoProviderError("关键帧数量超过限制")
        keyframes: list[Keyframe] = []
        artifacts: list[StoredArtifact] = []
        events: list[VisualEvent] = []
        keyframe_ids: set[str] = set()
        image_hashes: set[str] = set()
        previous_start = -1
        for index, sample in enumerate(samples):
            if sample.keyframe.keyframe_id in keyframe_ids:
                raise VideoProviderError("关键帧标识重复")
            keyframe_ids.add(sample.keyframe.keyframe_id)
            sample.keyframe.validate_against_duration(metadata.duration_ms)
            if not sample.image or len(sample.image) > self.settings.video_max_bytes:
                raise VideoProviderError("关键帧文件超过大小限制")
            image_digest = sha256(sample.image).hexdigest()
            if image_digest in image_hashes:
                continue
            image_hashes.add(image_digest)
            stored = self.artifacts.put_bytes(sample.image, ".webp")
            artifacts.append(stored)
            keyframe = sample.keyframe.model_copy(
                update={
                    "artifact_id": None,
                    "relative_path": stored.relative_path,
                    "content_hash": stored.content_hash,
                }
            )
            keyframes.append(keyframe)
            await self._job_stage(job_id, "vision", 0.78 + 0.02 * min(index, 10))
            produced = (
                list(await self.vision_provider.analyze(sample.image, keyframe=keyframe))
                if self.vision_provider is not None
                else []
            )
            for event in produced:
                event.validate_against_duration(metadata.duration_ms)
                if event.start_ms < previous_start:
                    raise VideoProviderError("视觉事件顺序无效")
                if not event.keyframe_ids or not set(event.keyframe_ids).issubset(keyframe_ids):
                    raise VideoProviderError("视觉事件引用了未知关键帧")
                if any(ord(char) < 32 and char not in {"\t", "\n", "\r"} for char in event.summary):
                    raise VideoTextError("视觉摘要包含控制字符")
                if re.search(r"(?is)<\s*(?:script|iframe|object|embed)|javascript\s*:", event.summary):
                    raise VideoTextError("视觉摘要包含不安全内容")
                events.append(event)
                previous_start = event.start_ms
            if self.ocr_provider is not None:
                ocr_text = await self.ocr_provider.extract(sample.image, keyframe=keyframe)
                if ocr_text is not None:
                    ocr_text = " ".join(ocr_text.split())
                    if not ocr_text or len(ocr_text) > 10_000:
                        raise VideoTextError("OCR 文本无效")
                    if any(
                        ord(char) < 32 and char not in {"\t", "\n", "\r"}
                        for char in ocr_text
                    ) or re.search(r"(?is)<\s*(?:script|iframe|object|embed)|javascript\s*:", ocr_text):
                        raise VideoTextError("OCR 文本包含不安全内容")
                    ocr_event = VisualEvent(
                        start_ms=keyframe.start_ms,
                        end_ms=keyframe.end_ms,
                        duration_ms=keyframe.duration_ms,
                        event_type="other",
                        summary=f"OCR 文字：{ocr_text}",
                        keyframe_ids=[keyframe.keyframe_id],
                    )
                    events.append(ocr_event)
        events.sort(key=lambda event: (event.start_ms, event.end_ms, event.summary))
        return (
            events,
            keyframes,
            artifacts,
            getattr(self.vision_provider, "model", None),
            getattr(self.ocr_provider, "model", None) if self.ocr_provider else None,
        )

    async def _persist_video_result(
        self,
        item_id: str,
        source_artifact_id: str,
        body: str,
        metadata: VideoSourceMetadata,
        metadata_segments: list[dict[str, object]],
        manifest: ProcessingManifest,
        *,
        media_stored: StoredArtifact | None,
        subtitle_stored: StoredArtifact | None,
        transcript_stored: StoredArtifact,
        keyframe_artifacts: Sequence[StoredArtifact],
        title_provided: bool,
        source_kind: str,
        source_url_hash: str,
        subtitle_track: SubtitleTrack | None,
        job_id: str,
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session, session.begin():
            item = await session.get(KnowledgeItem, item_id)
            source_artifact = await session.get(SourceArtifact, source_artifact_id)
            job = await session.get(ProcessingJob, job_id)
            if item is None or source_artifact is None or item.source_type != "video":
                raise VideoProcessingError("视频知识条目不存在")
            if job is None or job.state == "cancelled":
                raise VideoJobCancelled("视频任务已取消")
            source_metadata = self._json_object(source_artifact.metadata_json) or {}
            source_metadata.update(
                {
                    "final_url": metadata.final_url or metadata.source_url,
                    "provider": metadata.provider,
                    "tool_version": metadata.tool_version,
                }
            )
            source_artifact.metadata_json = json.dumps(source_metadata, ensure_ascii=False, sort_keys=True)

            media_row = None
            if media_stored is not None:
                media_row = SourceArtifact(
                    knowledge_item_id=item.id,
                    artifact_type="video_media",
                    media_type=self._media_type(metadata.media_type),
                    relative_path=media_stored.relative_path,
                    content_hash=media_stored.content_hash,
                    byte_size=media_stored.byte_size,
                    source_locator=json.dumps(
                        {"kind": "video_media", "url": metadata.final_url or metadata.source_url},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    metadata_json=json.dumps(
                        {"provider": metadata.provider, "tool_version": metadata.tool_version},
                        ensure_ascii=False,
                    ),
                    retention_policy=self.settings.video_media_retention_policy,
                    retention_expires_at=(
                        now + timedelta(days=self.settings.video_media_retention_days)
                        if self.settings.video_media_retention_policy == "until_expiry"
                        else None
                    ),
                )
                session.add(media_row)

            subtitle_row = None
            if subtitle_stored is not None:
                subtitle_row = SourceArtifact(
                    knowledge_item_id=item.id,
                    artifact_type="video_subtitle",
                    media_type="text/vtt" if subtitle_stored.relative_path.endswith(".vtt") else "text/plain",
                    relative_path=subtitle_stored.relative_path,
                    content_hash=subtitle_stored.content_hash,
                    byte_size=subtitle_stored.byte_size,
                    source_locator=json.dumps(
                        {"kind": "video_subtitle", "url": metadata.final_url or metadata.source_url},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    metadata_json=json.dumps(
                        {
                            "provider": subtitle_track.provider if subtitle_track else metadata.provider,
                            "tool_version": subtitle_track.tool_version
                            if subtitle_track
                            else metadata.tool_version,
                            "language": subtitle_track.language if subtitle_track else None,
                            "is_automatic": subtitle_track.is_automatic if subtitle_track else False,
                            "source_url": subtitle_track.source_url
                            if subtitle_track and subtitle_track.source_url
                            else metadata.final_url or metadata.source_url,
                        },
                        ensure_ascii=False,
                    ),
                    retention_policy="permanent",
                )
                session.add(subtitle_row)

            transcript_row = SourceArtifact(
                knowledge_item_id=item.id,
                artifact_type="video_transcript",
                media_type="text/vtt",
                relative_path=transcript_stored.relative_path,
                content_hash=transcript_stored.content_hash,
                byte_size=transcript_stored.byte_size,
                source_locator=json.dumps(
                    {"kind": "video_transcript", "url": metadata.final_url or metadata.source_url},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                metadata_json=json.dumps(
                    {
                        "provider": subtitle_track.provider if subtitle_track else metadata.provider,
                        "tool_version": subtitle_track.tool_version
                        if subtitle_track
                        else metadata.tool_version,
                        "language": subtitle_track.language if subtitle_track else None,
                        "source_url": subtitle_track.source_url
                        if subtitle_track and subtitle_track.source_url
                        else metadata.final_url or metadata.source_url,
                    },
                    ensure_ascii=False,
                ),
                retention_policy="permanent",
            )
            session.add(transcript_row)

            keyframe_rows: list[SourceArtifact] = []
            for stored in keyframe_artifacts:
                row = SourceArtifact(
                    knowledge_item_id=item.id,
                    artifact_type="video_keyframe",
                    media_type="image/webp",
                    relative_path=stored.relative_path,
                    content_hash=stored.content_hash,
                    byte_size=stored.byte_size,
                    source_locator=json.dumps(
                        {"kind": "video_keyframe", "url": metadata.final_url or metadata.source_url},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    retention_policy="permanent",
                )
                session.add(row)
                keyframe_rows.append(row)
            await session.flush()

            persisted_keyframes = [
                keyframe.model_copy(update={"artifact_id": row.id})
                for keyframe, row in zip(manifest.keyframes, keyframe_rows, strict=True)
            ]
            persisted_manifest = manifest.model_copy(
                update={
                    "keyframes": persisted_keyframes,
                    "media_artifact_id": media_row.id if media_row else None,
                    "transcript_artifact_id": transcript_row.id,
                    "subtitle_artifact_ids": [subtitle_row.id] if subtitle_row else [],
                }
            )
            persisted_segments = self._persisted_locators(metadata_segments, transcript_row.id)
            version_metadata: dict[str, object] = {
                "kind": "video",
                "source_type": "video",
                "url": metadata.final_url or metadata.source_url,
                "source_url": metadata.requested_url or metadata.source_url,
                "requested_url": metadata.requested_url or metadata.source_url,
                "final_url": metadata.final_url or metadata.source_url,
                "source_artifact_id": source_artifact.id,
                "media_artifact_id": media_row.id if media_row else None,
                "subtitle_artifact_id": subtitle_row.id if subtitle_row else None,
                "source_url_hash": source_url_hash,
                "transcript_language": subtitle_track.language
                if subtitle_track
                else next((segment.language for segment in manifest.transcript_segments if segment.language), None),
                "transcript_artifact_id": transcript_row.id,
                "manifest": persisted_manifest.model_dump(mode="json"),
                "video": metadata.model_dump(mode="json"),
                "segments": persisted_segments,
            }
            version = ContentVersion(
                knowledge_item_id=item.id,
                version_no=await self._next_version_no(session, item.id),
                source_kind=source_kind,
                title=(item.title if title_provided else (metadata.title or item.title))[:300],
                body=normalize_content(body),
                content_hash=content_hash(body),
                summary="字幕优先采集" if source_kind == "video_subtitle" else "自动转录结果待确认",
                suggested_tags_json="[]",
                prompt_version="video-processing-v1",
                source_metadata_json=json.dumps(version_metadata, ensure_ascii=False),
            )
            session.add(version)
            await session.flush()
            item.pending_content_version_id = version.id
            if not item.current_content_version_id:
                item.status = "pending_review"
                if not title_provided:
                    item.title = version.title
            else:
                # A published/current version remains authoritative while the
                # new video result waits for review.
                item.status = "published"
            item.updated_at = now
            return {
                "item_id": item.id,
                "content_version_id": version.id,
                "source_type": "video",
                "status": manifest.processing_status,
                "subtitle_available": subtitle_row is not None,
                "transcript_artifact_id": transcript_row.id,
                "media_artifact_id": media_row.id if media_row else None,
                "subtitle_artifact_id": subtitle_row.id if subtitle_row else None,
                "chapter_count": len(manifest.chapters),
                "visual_event_count": len(manifest.visual_events),
                "source_url_hash": source_url_hash,
            }

    @staticmethod
    def _media_type(value: str | None) -> str:
        if isinstance(value, str) and re.fullmatch(r"video/[a-z0-9.+-]+", value.casefold()):
            return value.casefold()
        return "video/mp4"

    @staticmethod
    def _persisted_locators(
        metadata_segments: Sequence[dict[str, object]], transcript_artifact_id: str
    ) -> list[dict[str, object]]:
        persisted: list[dict[str, object]] = []
        for segment in metadata_segments:
            copied = dict(segment)
            locator = copied.get("locator")
            if isinstance(locator, dict) and locator.get("kind") == "video":
                locator_copy = dict(locator)
                locator_copy["transcript_artifact_id"] = transcript_artifact_id
                copied["locator"] = locator_copy
            persisted.append(copied)
        return persisted

    async def _next_version_no(self, session: AsyncSession, item_id: str) -> int:
        result = await session.execute(
            select(func.max(ContentVersion.version_no)).where(
                ContentVersion.knowledge_item_id == item_id
            )
        )
        return int(result.scalar_one() or 0) + 1

    async def _job_stage(self, job_id: str, stage: str, progress: float) -> None:
        async with self.session_factory() as session, session.begin():
            job = await session.get(ProcessingJob, job_id)
            if job is None or job.state == "cancelled":
                raise VideoJobCancelled("视频任务已取消")
            if job.state == "running":
                job.stage = stage[:80]
                job.progress = max(0.0, min(1.0, progress))
                job.heartbeat_at = datetime.now(timezone.utc)

    async def _check_cancelled(self, job_id: str) -> None:
        async with self.session_factory() as session:
            job = await session.get(ProcessingJob, job_id)
        if job is None or job.state == "cancelled":
            raise VideoJobCancelled("视频任务已取消")

    async def _mark_failed_item(self, item_id: str) -> None:
        async with self.session_factory() as session, session.begin():
            item = await session.get(KnowledgeItem, item_id)
            if item is not None and item.current_content_version_id is None:
                item.status = "failed"
                item.updated_at = datetime.now(timezone.utc)

    async def _cleanup_staged(self, staged: Sequence[StoredArtifact]) -> None:
        paths = list(dict.fromkeys(artifact.relative_path for artifact in staged))
        if not paths:
            return
        async with self.session_factory() as session:
            referenced = set(
                (
                    await session.execute(
                        select(SourceArtifact.relative_path).where(
                            SourceArtifact.relative_path.in_(paths)
                        )
                    )
                ).scalars()
            )
        for artifact in staged:
            if artifact.relative_path not in referenced and self.artifacts.exists(artifact.relative_path):
                try:
                    self.artifacts.delete(artifact.relative_path)
                except OSError:
                    pass

    async def _cleanup_artifact(self, stored: StoredArtifact, *, immediate: bool) -> bool:
        async with self.session_factory() as session, session.begin():
            artifact = (
                await session.execute(
                    select(SourceArtifact)
                    .where(
                        SourceArtifact.relative_path == stored.relative_path,
                        SourceArtifact.content_hash == stored.content_hash,
                    )
                    .order_by(SourceArtifact.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if artifact is None:
                return False
            references = int(
                (
                    await session.execute(
                        select(func.count(SourceArtifact.id)).where(
                            SourceArtifact.relative_path == stored.relative_path,
                            SourceArtifact.cleanup_state != "deleted",
                            SourceArtifact.id != artifact.id,
                        )
                    )
                ).scalar_one()
            )
            try:
                # Content-addressed files can be referenced by more than one
                # processing attempt.  Retire this row even when another
                # active row still owns the physical file; only the last
                # active reference is allowed to unlink it.
                if references == 0 and self.artifacts.exists(stored.relative_path):
                    self.artifacts.delete(stored.relative_path)
            except OSError:
                artifact.cleanup_state = "failed"
                artifact.cleaned_at = None
                return False
            artifact.cleanup_state = "deleted"
            artifact.cleaned_at = datetime.now(timezone.utc)
            return True

    async def cleanup_due(self, now: datetime | None = None) -> dict[str, int]:
        current = now or datetime.now(timezone.utc)
        async with self.session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(SourceArtifact).where(
                            SourceArtifact.artifact_type == "video_media",
                            SourceArtifact.cleanup_state != "deleted",
                            (
                                (SourceArtifact.cleanup_state == "due")
                                | (
                                    (SourceArtifact.retention_policy == "until_expiry")
                                    & (SourceArtifact.retention_expires_at <= current)
                                )
                            ),
                        )
                    )
                ).scalars()
            )
        deleted = failed = 0
        for row in rows:
            ok = await self._cleanup_artifact(
                StoredArtifact(row.relative_path, row.content_hash, row.byte_size),
                immediate=True,
            )
            if ok:
                deleted += 1
            else:
                failed += 1
        return {"scanned": len(rows), "deleted": deleted, "failed": failed}

    @asynccontextmanager
    async def _temporary_directory(self):
        self.artifacts.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".video-", dir=self.artifacts.root) as raw:
            yield Path(raw)
