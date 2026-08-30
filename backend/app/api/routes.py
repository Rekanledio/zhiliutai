import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from fastapi import APIRouter, Body, File, Form, Query, Request, UploadFile
from sqlalchemy import and_, func, or_, select

from app.core.errors import ApplicationError
from app.core.paths import safe_relative_path
from app.core.safety import redact_sensitive_text, redact_sensitive_value
from app.db.models import (
    ContentVersion,
    Collection,
    CollectionItem,
    JobAttempt,
    KnowledgeItem,
    KnowledgeItemTag,
    NoteBinding,
    ProcessingJob,
    SourceArtifact,
    Tag,
)
from app.obsidian.state import watcher_state
from app.schemas.collections import (
    CollectionResponse,
    CollectionSummaryResponse,
    CollectionUpdateRequest,
    CollectionWriteRequest,
    normalize_collection_names,
)
from app.schemas.health import (
    DashboardJob,
    DashboardPendingReview,
    DashboardRecentItem,
    DashboardResponse,
    DashboardStats,
    HealthResponse,
)
from app.schemas.settings import (
    MaintenanceRequest,
    SettingsBackupResponse,
    SettingsRebuildResponse,
    SettingsRescanResponse,
    SettingsResponse,
)
from app.schemas.stage2 import (
    ItemPatchRequest,
    ItemResponse,
    JobAttemptResponse,
    JobResponse,
    ObsidianOpenResponse,
    ObsidianStatusResponse,
    PublishRequest,
    ReviewRequest,
    SubmissionResponse,
    TextSourceRequest,
    UrlSourceRequest,
)
from app.schemas.tags import normalize_tag_names
from app.schemas.video import VideoSourceRequest
from app.services.health import build_health_report
from app.services.backup import BackupError
from app.services.maintenance import MaintenanceBusyError
from app.services.settings import build_settings_response
from app.services.stage2 import Stage2Service
from app.workflows.contracts import IngestionResumeDecision
from app.workflows.production import _safe_job_result as production_safe_job_result

router = APIRouter(prefix="/api")

_SAFE_SOURCE_METADATA_FIELDS = frozenset(
    {
        "kind",
        "source_type",
        "media_type",
        "title",
        "html_title",
        "url",
        "source_url",
        "requested_url",
        "final_url",
        "page_count",
        "paragraph_count",
        "table_count",
        "heading_count",
        "heading_paragraphs",
        "table_row_counts",
        "duration_ms",
        "video_id",
        "uploader",
        "width_px",
        "height_px",
        "frame_rate",
        "is_live",
        "video_kind",
        "transcript_language",
        "source_url_hash",
        "provider",
        "tool_version",
    }
)
_SOURCE_URL_FIELDS = frozenset({"url", "source_url", "requested_url", "final_url"})
_ITEM_STATUSES = frozenset({"processing", "pending_review", "reviewed", "published", "failed"})
_ITEM_SOURCE_TYPES = frozenset({"text", "markdown", "pdf", "docx", "webpage", "video"})


def _safe_public_source_url(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
    except ValueError:
        return None
    # Query and fragment values are intentionally omitted from public item
    # metadata because source URLs may contain tracking or authentication
    # material even when the intake validator accepted the URL.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, "", ""))


def _safe_metadata_value(value: object, *, key: str | None = None) -> object:
    if key in _SOURCE_URL_FIELDS:
        return _safe_public_source_url(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): _safe_metadata_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
            if isinstance(child_key, str)
        }
    if isinstance(value, list):
        return [_safe_metadata_value(child) for child in value]
    return redact_sensitive_value(value)


def _safe_source_metadata(value: Mapping[str, object]) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key in _SAFE_SOURCE_METADATA_FIELDS:
        if key not in value:
            continue
        sanitized = _safe_metadata_value(value[key], key=key)
        if sanitized is not None:
            safe[key] = sanitized

    raw_segments = value.get("segments")
    if isinstance(raw_segments, list):
        segments: list[dict[str, object]] = []
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, Mapping):
                continue
            locator = raw_segment.get("locator")
            if not isinstance(locator, Mapping):
                continue
            segments.append(
                {"locator": _safe_metadata_value(locator)}
            )
        safe["segments"] = segments
    return safe


def _item_filter_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    try:
        if len(normalized) == 10:
            parsed_date = date.fromisoformat(normalized)
            parsed = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
            if end_of_day:
                parsed += timedelta(days=1)
        else:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
    except ValueError as error:
        raise ApplicationError(422, "invalid_item_filter", "知识库日期筛选条件无效") from error
    return parsed


def service(request: Request) -> Stage2Service:
    return request.app.state.stage2_service


def _backup_application_error(error: BackupError) -> ApplicationError:
    errors = {
        "vault_not_configured": (
            409,
            "vault_not_configured",
            "尚未配置 Obsidian Vault，无法执行此操作",
        ),
        "database_missing": (503, "database_unavailable", "业务数据库尚不可用"),
        "schema_incompatible": (503, "database_incompatible", "业务数据库版本不兼容"),
        "embedding_not_configured": (
            503,
            "embedding_not_configured",
            "Embedding 尚未配置，无法重建派生索引",
        ),
        "artifact_state_invalid": (503, "derived_state_invalid", "Artifact 状态无法验证"),
        "vault_state_invalid": (503, "derived_state_invalid", "Obsidian 状态无法验证"),
        "backup_target_exists": (503, "backup_target_exists", "备份目标已存在，请稍后重试"),
    }
    status_code, code, message = errors.get(
        error.code,
        (503, "maintenance_failed", "维护操作未完成"),
    )
    return ApplicationError(status_code, code, message)


def _maintenance_busy_error() -> ApplicationError:
    return ApplicationError(409, "maintenance_busy", "已有维护操作正在执行")


async def _rescan_with_maintenance(request: Request) -> SettingsRescanResponse:
    try:
        result = await request.app.state.maintenance_service.rescan(wait_if_busy=True)
    except MaintenanceBusyError as error:
        raise _maintenance_busy_error() from error
    assert result is not None
    return SettingsRescanResponse(**result)


@router.get("/settings", response_model=SettingsResponse, tags=["system"])
async def settings(request: Request) -> SettingsResponse:
    return build_settings_response(
        request.app.state.settings,
        embedding_provider=request.app.state.embedding_provider,
    )


@router.post(
    "/settings/rescan",
    response_model=SettingsRescanResponse,
    tags=["system"],
)
async def settings_rescan(
    request: Request,
    _payload: MaintenanceRequest | None = Body(default=None),
) -> SettingsRescanResponse:
    return await _rescan_with_maintenance(request)


@router.post(
    "/settings/rebuild",
    response_model=SettingsRebuildResponse,
    tags=["system"],
)
async def settings_rebuild(
    request: Request,
    _payload: MaintenanceRequest | None = Body(default=None),
) -> SettingsRebuildResponse:
    try:
        result = await request.app.state.maintenance_service.rebuild()
    except MaintenanceBusyError as error:
        raise _maintenance_busy_error() from error
    except BackupError as error:
        raise _backup_application_error(error) from error
    return SettingsRebuildResponse(
        published_items=result.published_items,
        chunks=result.chunks,
    )


@router.post(
    "/settings/backup",
    response_model=SettingsBackupResponse,
    status_code=201,
    tags=["system"],
)
async def settings_backup(
    request: Request,
    _payload: MaintenanceRequest | None = Body(default=None),
) -> SettingsBackupResponse:
    archive_id = f"backup-{uuid4().hex}"
    destination = request.app.state.settings.backup_root / f"{archive_id}.zip"
    try:
        result = await request.app.state.maintenance_service.backup(destination)
    except MaintenanceBusyError as error:
        raise _maintenance_busy_error() from error
    except BackupError as error:
        raise _backup_application_error(error) from error
    return SettingsBackupResponse(
        archive_id=archive_id,
        created_at=result.manifest.created_at,
        sha256=result.archive_sha256,
    )


@router.get(
    "/collections",
    response_model=list[CollectionSummaryResponse],
    tags=["collections"],
)
async def list_collections(request: Request) -> list[CollectionSummaryResponse]:
    return await request.app.state.knowledge_service.list_collections()


@router.post(
    "/collections",
    response_model=CollectionResponse,
    status_code=201,
    tags=["collections"],
)
async def create_collection(
    payload: CollectionWriteRequest, request: Request
) -> CollectionResponse:
    return await request.app.state.knowledge_service.create_collection(
        payload.name, payload.description
    )


@router.get(
    "/collections/{collection_id}",
    response_model=CollectionResponse,
    tags=["collections"],
)
async def get_collection(
    collection_id: str, request: Request
) -> CollectionResponse:
    return await request.app.state.knowledge_service.get_collection(collection_id)


@router.patch(
    "/collections/{collection_id}",
    response_model=CollectionResponse,
    tags=["collections"],
)
async def update_collection(
    collection_id: str,
    payload: CollectionUpdateRequest,
    request: Request,
) -> CollectionResponse:
    updates: dict[str, object] = {}
    if "name" in payload.model_fields_set:
        updates["name"] = payload.name
    if "description" in payload.model_fields_set:
        updates["description"] = payload.description
    return await request.app.state.knowledge_service.update_collection(
        collection_id, **updates
    )


@router.delete("/collections/{collection_id}", status_code=204, tags=["collections"])
async def delete_collection(collection_id: str, request: Request) -> None:
    await request.app.state.knowledge_service.delete_collection(collection_id)


@router.post(
    "/collections/{collection_id}/items/{item_id}",
    response_model=CollectionResponse,
    tags=["collections"],
)
async def add_collection_item(
    collection_id: str, item_id: str, request: Request
) -> CollectionResponse:
    return await request.app.state.knowledge_service.add_collection_item(
        collection_id, item_id
    )


@router.delete(
    "/collections/{collection_id}/items/{item_id}",
    response_model=CollectionResponse,
    tags=["collections"],
)
async def remove_collection_item(
    collection_id: str, item_id: str, request: Request
) -> CollectionResponse:
    return await request.app.state.knowledge_service.remove_collection_item(
        collection_id, item_id
    )


def _safe_job_error(value: str | None) -> dict[str, object] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    if not isinstance(parsed, Mapping):
        parsed = {}
    safe: dict[str, object] = {}
    code = parsed.get("code")
    if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum():
        safe["code"] = code[:80]
    error_type = parsed.get("type")
    if isinstance(error_type, str):
        safe_type = redact_sensitive_text(error_type).replace("\r", " ").replace("\n", " ").strip()
        if safe_type and len(safe_type) <= 120:
            safe["type"] = safe_type
    message = parsed.get("message")
    if isinstance(message, str):
        safe_message = redact_sensitive_text(message).replace("\r", " ").replace("\n", " ").strip()
        lowered = safe_message.casefold()
        if (
            not safe_message
            or "traceback" in lowered
            or "stack trace" in lowered
            or "http://" in lowered
            or "https://" in lowered
            or "?" in safe_message
        ):
            safe_message = "处理失败"
        safe["message"] = safe_message[:300]
    if not safe:
        return {"code": "job_failed", "message": "处理失败"}
    safe.setdefault("message", "处理失败")
    return safe


def _safe_job_result(value: str | None) -> dict[str, object] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    safe = _safe_job_result_fields(parsed)
    return safe or None


def _safe_job_result_fields(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    # Keep the same narrow result allowlist used by the production workflow;
    # job payloads, URLs, paths and provider responses never cross this API.
    return production_safe_job_result(value)


def _duration_ms(
    started_at: datetime | None,
    finished_at: datetime | None,
    now: datetime,
) -> int | None:
    if started_at is None:
        return None
    start = started_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = finished_at or now
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds() * 1000))


def _job_attempt_out(attempt: JobAttempt, now: datetime) -> JobAttemptResponse:
    return JobAttemptResponse(
        id=attempt.id,
        attempt_no=attempt.attempt_no,
        state=attempt.state,
        stage=attempt.stage,
        started_at=attempt.started_at,
        heartbeat_at=attempt.heartbeat_at,
        finished_at=attempt.finished_at,
        duration_ms=_duration_ms(attempt.started_at, attempt.finished_at, now),
        error=_safe_job_error(attempt.error_json),
    )


def job_out(
    job: ProcessingJob,
    attempts: list[JobAttempt] | None = None,
    *,
    now: datetime | None = None,
) -> JobResponse:
    observed_at = now or datetime.now(timezone.utc)
    return JobResponse(
        id=job.id,
        kind=job.kind,
        state=job.state,
        stage=job.stage,
        progress=max(0.0, min(1.0, float(job.progress))),
        retry_count=job.retry_count,
        max_retries=job.max_retries,
        error=_safe_job_error(job.error_json),
        result=_safe_job_result(job.result_json),
        heartbeat_at=job.heartbeat_at,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_ms=_duration_ms(job.started_at, job.finished_at, observed_at),
        attempts=[
            _job_attempt_out(attempt, observed_at)
            for attempt in (attempts or [])
        ],
    )


async def _job_attempts(
    session, job_ids: list[str]
) -> dict[str, list[JobAttempt]]:
    if not job_ids:
        return {}
    result = await session.execute(
        select(JobAttempt)
        .where(JobAttempt.processing_job_id.in_(job_ids))
        .order_by(JobAttempt.processing_job_id, JobAttempt.attempt_no)
    )
    grouped: dict[str, list[JobAttempt]] = {}
    for attempt in result.scalars():
        grouped.setdefault(attempt.processing_job_id, []).append(attempt)
    return grouped


async def _job_response(request: Request, job_id: str) -> JobResponse:
    async with request.app.state.session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        if job is None:
            raise ApplicationError(404, "job_not_found", "任务不存在")
        attempts = await _job_attempts(session, [job.id])
        return job_out(job, attempts.get(job.id), now=datetime.now(timezone.utc))


def _local_timestamp(value: datetime, local_timezone) -> datetime:
    timestamp = value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(local_timezone)


def item_out(
    item: KnowledgeItem,
    version: ContentVersion | None = None,
    binding: NoteBinding | None = None,
    *,
    confirmed_tags: list[str] | None = None,
    collections: list[str] | None = None,
) -> ItemResponse:
    tags: list[str] = []
    suggested_collections: list[str] = []
    source_metadata: dict[str, object] | None = None
    if version is not None:
        try:
            parsed = json.loads(version.suggested_tags_json or "[]")
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            try:
                tags = normalize_tag_names(parsed)
            except ValueError:
                tags = []
        try:
            parsed_collections = json.loads(version.suggested_collections_json or "[]")
        except json.JSONDecodeError:
            parsed_collections = []
        if isinstance(parsed_collections, list):
            try:
                suggested_collections = normalize_collection_names(parsed_collections)
            except ValueError:
                suggested_collections = []
        try:
            parsed_metadata = json.loads(version.source_metadata_json or "{}")
        except json.JSONDecodeError:
            parsed_metadata = {}
        if isinstance(parsed_metadata, dict):
            source_metadata = _safe_source_metadata(parsed_metadata)
    safe_path = safe_relative_path(binding.relative_path) if binding else None
    return ItemResponse(
        id=item.id,
        title=version.title if version is not None else item.title,
        source_type=item.source_type,
        status=item.status,
        content_hash=version.content_hash if version else item.content_hash,
        current_content_version_id=item.current_content_version_id,
        pending_content_version_id=item.pending_content_version_id,
        has_pending_review=(
            item.pending_content_version_id is not None
            or item.status == "pending_review"
        ),
        body=version.body if version else None,
        summary=version.summary if version else None,
        suggested_tags=tags,
        suggested_collections=suggested_collections,
        confirmed_tags=confirmed_tags or [],
        collections=collections or [],
        source_metadata=source_metadata,
        version_no=version.version_no if version else None,
        note_relative_path=safe_path,
        sync_state=binding.sync_state if binding else None,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def item_response(
    request: Request,
    item: KnowledgeItem,
    version: ContentVersion | None = None,
    binding: NoteBinding | None = None,
) -> ItemResponse:
    confirmed_tags, collections = await service(request).get_item_organization(item.id)
    return item_out(
        item,
        version,
        binding,
        confirmed_tags=confirmed_tags,
        collections=collections,
    )


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(request: Request) -> HealthResponse:
    return await build_health_report(request.app.state.settings)


@router.get("/dashboard", response_model=DashboardResponse, tags=["dashboard"])
async def dashboard(request: Request) -> DashboardResponse:
    now = datetime.now().astimezone()
    greeting = (
        "夜深了"
        if now.hour < 6
        else "早上好"
        if now.hour < 12
        else "下午好"
        if now.hour < 18
        else "晚上好"
    )
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        active_items = list(
            (
                await session.execute(
                    select(KnowledgeItem).where(KnowledgeItem.deleted_at.is_(None))
                )
            ).scalars()
        )
        knowledge_count = sum(
            item.status == "published" and item.current_content_version_id is not None
            for item in active_items
        )
        today_added = sum(
            _local_timestamp(item.created_at, now.tzinfo).date() == now.date()
            for item in active_items
        )
        pending_clause = or_(
            KnowledgeItem.status.in_(["pending_review", "reviewed"]),
            and_(
                KnowledgeItem.status == "published",
                KnowledgeItem.pending_content_version_id.is_not(None),
            ),
        )
        pending_count = int(
            (
                await session.execute(
                    select(func.count(KnowledgeItem.id)).where(
                        pending_clause,
                        KnowledgeItem.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
        )
        processing_count = int(
            (
                await session.execute(
                    select(func.count(ProcessingJob.id)).where(
                        ProcessingJob.state.in_(["queued", "running"])
                    )
                )
            ).scalar_one()
        )
        pending_items = list(
            (
                await session.execute(
                    select(KnowledgeItem)
                    .where(
                        pending_clause,
                        KnowledgeItem.deleted_at.is_(None),
                    )
                    .order_by(KnowledgeItem.updated_at.desc())
                    .limit(5)
                )
            ).scalars()
        )
        recent_items = list(
            (
                await session.execute(
                    select(KnowledgeItem)
                    .where(
                        KnowledgeItem.status == "published",
                        KnowledgeItem.current_content_version_id.is_not(None),
                        KnowledgeItem.deleted_at.is_(None),
                    )
                    .order_by(KnowledgeItem.updated_at.desc())
                    .limit(5)
                )
            ).scalars()
        )
        jobs = list(
            (
                await session.execute(
                    select(ProcessingJob)
                    .where(ProcessingJob.state.in_(["queued", "running", "failed"]))
                    .order_by(ProcessingJob.created_at.desc())
                    .limit(5)
                )
            ).scalars()
        )
    return DashboardResponse(
        greeting=greeting,
        date_label=f"{now.month} 月 {now.day} 日 · {weekdays[now.weekday()]}",
        stats=DashboardStats(
            knowledge_count=knowledge_count,
            today_added=today_added,
            pending_review=pending_count,
            processing=processing_count,
        ),
        health=await build_health_report(request.app.state.settings),
        pending_reviews=[
            DashboardPendingReview(
                id=item.id,
                title=redact_sensitive_text(item.title),
                source_type=item.source_type,
                status=item.status,
                updated_at=item.updated_at,
            )
            for item in pending_items
        ],
        recent_items=[
            DashboardRecentItem(
                id=item.id,
                title=redact_sensitive_text(item.title),
                source_type=item.source_type,
                status=item.status,
                updated_at=item.updated_at,
            )
            for item in recent_items
        ],
        processing_jobs=[
            DashboardJob(
                id=job.id,
                kind=job.kind,
                state=job.state,
                stage=job.stage,
                progress=max(0.0, min(1.0, float(job.progress))),
                heartbeat_at=job.heartbeat_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                duration_ms=_duration_ms(job.started_at, job.finished_at, now),
                error=_safe_job_error(job.error_json),
            )
            for job in jobs
        ],
    )


@router.post(
    "/sources/text", response_model=SubmissionResponse, status_code=202, tags=["sources"]
)
async def add_text(payload: TextSourceRequest, request: Request) -> SubmissionResponse:
    item, job, deduplicated = await service(request).submit_text(
        payload.content,
        payload.source_type,
        payload.title,
        payload.idempotency_key,
    )
    return SubmissionResponse(
        item_id=item.id, job_id=job.id, deduplicated=deduplicated
    )


@router.post(
    "/sources/files", response_model=SubmissionResponse, status_code=202, tags=["sources"]
)
async def add_file(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=300),
    idempotency_key: str | None = Form(default=None, min_length=1, max_length=200),
) -> SubmissionResponse:
    settings = request.app.state.settings
    content = await file.read(settings.source_max_bytes + 1)
    item, job, deduplicated = await service(request).submit_file(
        content,
        file.filename,
        file.content_type,
        title,
        idempotency_key,
    )
    return SubmissionResponse(
        item_id=item.id, job_id=job.id, deduplicated=deduplicated
    )


@router.post(
    "/sources/url", response_model=SubmissionResponse, status_code=202, tags=["sources"]
)
async def add_url(
    payload: UrlSourceRequest, request: Request
) -> SubmissionResponse:
    item, job, deduplicated = await service(request).submit_url(
        payload.url,
        payload.title,
        payload.idempotency_key,
    )
    return SubmissionResponse(
        item_id=item.id, job_id=job.id, deduplicated=deduplicated
    )


@router.post(
    "/sources/video", response_model=SubmissionResponse, status_code=202, tags=["sources"]
)
async def add_video(
    payload: VideoSourceRequest, request: Request
) -> SubmissionResponse:
    item, job, deduplicated = await service(request).submit_video(
        payload.url,
        payload.title,
        payload.language,
        payload.idempotency_key,
        payload.enable_vision,
    )
    return SubmissionResponse(
        item_id=item.id, job_id=job.id, deduplicated=deduplicated
    )


@router.get("/jobs", response_model=list[JobResponse], tags=["jobs"])
async def list_jobs(request: Request) -> list[JobResponse]:
    async with request.app.state.session_factory() as session:
        jobs = list(
            (
                await session.execute(
                    select(ProcessingJob).order_by(ProcessingJob.created_at.desc())
                )
            ).scalars()
        )
        attempts = await _job_attempts(session, [job.id for job in jobs])
        observed_at = datetime.now(timezone.utc)
        return [
            job_out(job, attempts.get(job.id), now=observed_at)
            for job in jobs
        ]


@router.get("/jobs/{job_id}", response_model=JobResponse, tags=["jobs"])
async def get_job(job_id: str, request: Request) -> JobResponse:
    return await _job_response(request, job_id)


@router.post("/jobs/{job_id}/retry", response_model=JobResponse, tags=["jobs"])
async def retry_job(job_id: str, request: Request) -> JobResponse:
    await request.app.state.job_runner.retry(job_id)
    return await _job_response(request, job_id)


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse, tags=["jobs"])
async def cancel_job(job_id: str, request: Request) -> JobResponse:
    job = await request.app.state.ingestion_workflow.cancel_job(job_id)
    if job is None:
        job = await request.app.state.job_runner.cancel(job_id)
    return await _job_response(request, job.id)


@router.get("/items", response_model=list[ItemResponse], tags=["items"])
async def list_items(
    request: Request,
    status: str | None = Query(default=None, max_length=32),
    source_type: str | None = Query(default=None, max_length=32),
    tag: str | None = Query(default=None, max_length=80),
    collection: str | None = Query(default=None, max_length=200),
    created_after: str | None = Query(default=None, max_length=64),
    created_before: str | None = Query(default=None, max_length=64),
) -> list[ItemResponse]:
    if status is not None and status not in _ITEM_STATUSES:
        raise ApplicationError(422, "invalid_item_filter", "知识库状态筛选条件无效")
    if source_type is not None and source_type not in _ITEM_SOURCE_TYPES:
        raise ApplicationError(422, "invalid_item_filter", "知识库来源筛选条件无效")
    try:
        tag_name = normalize_tag_names([tag])[0] if tag is not None else None
        collection_name = (
            normalize_collection_names([collection])[0]
            if collection is not None
            else None
        )
    except ValueError as error:
        raise ApplicationError(422, "invalid_item_filter", "知识库标签或合集筛选条件无效") from error
    after = _item_filter_date(created_after)
    before = _item_filter_date(created_before, end_of_day=True)
    if after is not None and before is not None and after >= before:
        raise ApplicationError(422, "invalid_item_filter", "知识库日期范围无效")

    async with request.app.state.session_factory() as session:
        statement = select(KnowledgeItem).where(KnowledgeItem.deleted_at.is_(None))
        if status:
            statement = statement.where(KnowledgeItem.status == status)
            if status == "published":
                statement = statement.where(
                    KnowledgeItem.current_content_version_id.is_not(None)
                )
        if source_type:
            statement = statement.where(KnowledgeItem.source_type == source_type)
        if tag_name is not None:
            statement = statement.where(
                KnowledgeItem.id.in_(
                    select(KnowledgeItemTag.knowledge_item_id)
                    .join(Tag, Tag.id == KnowledgeItemTag.tag_id)
                    .where(Tag.normalized_name == tag_name.casefold())
                )
            )
        if collection_name is not None:
            statement = statement.where(
                KnowledgeItem.id.in_(
                    select(CollectionItem.knowledge_item_id)
                    .join(Collection, Collection.id == CollectionItem.collection_id)
                    .where(func.lower(Collection.name) == collection_name.casefold())
                )
            )
        if after is not None:
            statement = statement.where(KnowledgeItem.created_at >= after)
        if before is not None:
            statement = statement.where(KnowledgeItem.created_at < before)
        items = list(
            (
                await session.execute(statement.order_by(KnowledgeItem.updated_at.desc()))
            ).scalars()
        )
    responses: list[ItemResponse] = []
    for item in items:
        current_item, version, binding = await service(request).get_item(item.id)
        responses.append(await item_response(request, current_item, version, binding))
    return responses


@router.get("/items/{item_id}", response_model=ItemResponse, tags=["items"])
async def get_item(item_id: str, request: Request) -> ItemResponse:
    item, version, binding = await service(request).get_item(item_id)
    return await item_response(request, item, version, binding)


@router.patch("/items/{item_id}", response_model=ItemResponse, tags=["items"])
async def patch_item(
    item_id: str, payload: ItemPatchRequest, request: Request
) -> ItemResponse:
    item = await service(request).patch_item(
        item_id, payload.title, payload.body, payload.expected_content_hash
    )
    item, version, binding = await service(request).get_item(item.id)
    return await item_response(request, item, version, binding)


@router.post("/items/{item_id}/review", response_model=ItemResponse, tags=["items"])
async def review_item(
    item_id: str, payload: ReviewRequest, request: Request
) -> ItemResponse:
    if payload.resolved_decision() == "approve" and any(
        value is not None
        for value in (
            payload.title,
            payload.body,
            payload.summary,
            payload.suggested_tags,
            payload.suggested_collections,
        )
    ):
        await service(request).update_pending_review(
            item_id,
            title=payload.title,
            body=payload.body,
            summary=payload.summary,
            suggested_tags=payload.suggested_tags,
            suggested_collections=payload.suggested_collections,
        )
    decision = IngestionResumeDecision(decision=payload.resolved_decision())
    run = await request.app.state.ingestion_workflow.resume_item(
        item_id,
        decision,
        gate="review",
    )
    if run.get("stage") == "failed":
        raise ApplicationError(500, "internal_error", "服务内部错误")
    item, version, binding = await service(request).get_item(item_id)
    return await item_response(request, item, version, binding)


@router.post("/items/{item_id}/publish", response_model=ItemResponse, tags=["items"])
async def publish_item(
    item_id: str,
    request: Request,
    payload: PublishRequest | None = None,
) -> ItemResponse:
    decision = IngestionResumeDecision(
        decision=payload.resolved_decision() if payload is not None else "approve"
    )
    run = await request.app.state.ingestion_workflow.resume_item(
        item_id,
        decision,
        gate="publish",
    )
    if run.get("stage") == "failed":
        raise ApplicationError(500, "internal_error", "服务内部错误")
    item, version, binding = await service(request).get_item(item_id)
    return await item_response(request, item, version, binding)


@router.post(
    "/items/{item_id}/reprocess", response_model=SubmissionResponse, tags=["items"]
)
async def reprocess_item(item_id: str, request: Request) -> SubmissionResponse:
    async with request.app.state.session_factory() as session:
        existing = await session.get(KnowledgeItem, item_id)
    if existing is None or existing.deleted_at is not None:
        raise ApplicationError(404, "item_not_found", "知识条目不存在")
    if existing.source_type == "video":
        item, job = await service(request).reprocess_video(item_id)
        return SubmissionResponse(item_id=item.id, job_id=job.id, deduplicated=False)
    async with request.app.state.session_factory() as session, session.begin():
        item = await session.get(KnowledgeItem, item_id)
        if item is None or item.deleted_at is not None:
            raise ApplicationError(404, "item_not_found", "知识条目不存在")
        artifact_result = await session.execute(
            select(SourceArtifact)
            .where(SourceArtifact.knowledge_item_id == item.id)
            .order_by(SourceArtifact.created_at)
            .limit(1)
        )
        artifact = artifact_result.scalar_one_or_none()
        if artifact is None:
            raise ApplicationError(409, "artifact_missing", "原始 Artifact 不存在")
        payload: dict[str, object] = {
            "item_id": item.id,
            "artifact_id": artifact.id,
            "source_type": item.source_type,
            "title_provided": True,
        }
        if item.source_type == "webpage":
            try:
                source_locator = json.loads(artifact.source_locator or "{}")
            except json.JSONDecodeError as error:
                raise ApplicationError(409, "source_url_missing", "网页来源 URL 不存在") from error
            url = source_locator.get("url") if isinstance(source_locator, dict) else None
            if not isinstance(url, str) or not url:
                raise ApplicationError(409, "source_url_missing", "网页来源 URL 不存在")
            payload["url"] = url
        job = ProcessingJob(
            kind="ingest_source",
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        session.add(job)
        await session.flush()
    return SubmissionResponse(item_id=item.id, job_id=job.id, deduplicated=False)


@router.post("/video/cleanup", response_model=dict[str, int], tags=["sources"])
async def cleanup_video_artifacts(request: Request) -> dict[str, int]:
    return await service(request).cleanup_video_artifacts()


@router.delete("/items/{item_id}", status_code=204, tags=["items"])
async def delete_item(item_id: str, request: Request) -> None:
    await service(request).soft_delete(item_id)


@router.get(
    "/obsidian/status", response_model=ObsidianStatusResponse, tags=["obsidian"]
)
async def obsidian_status(request: Request) -> ObsidianStatusResponse:
    settings = request.app.state.settings
    return ObsidianStatusResponse(
        configured=settings.vault_root is not None,
        watcher_running=watcher_state.running,
        managed_directory=settings.managed_vault_dir
        if settings.vault_root is not None
        else None,
        last_heartbeat_at=watcher_state.last_heartbeat_at,
        last_error=watcher_state.last_error,
    )


@router.post("/obsidian/rescan", response_model=SettingsRescanResponse, tags=["obsidian"])
async def rescan_obsidian(request: Request) -> SettingsRescanResponse:
    return await _rescan_with_maintenance(request)


@router.post(
    "/obsidian/open/{item_id}",
    response_model=ObsidianOpenResponse,
    tags=["obsidian"],
)
async def open_obsidian(item_id: str, request: Request) -> ObsidianOpenResponse:
    item, _version, binding = await service(request).get_item(item_id)
    if (
        binding is None
        or item.status != "published"
        or binding.knowledge_item_id != item.id
        or binding.zhiliu_id != item.id
        or binding.sync_state != "synced"
    ):
        raise ApplicationError(409, "note_not_published", "条目尚未绑定 Obsidian 笔记")
    vault = service(request).vault()
    try:
        if not vault.resolve(binding.relative_path).is_file():
            raise ApplicationError(404, "note_not_found", "Obsidian 笔记不可访问")
        uri = vault.uri(binding.relative_path)
    except (OSError, ValueError) as error:
        raise ApplicationError(404, "note_not_found", "Obsidian 笔记不可访问") from error
    return ObsidianOpenResponse(uri=uri)
