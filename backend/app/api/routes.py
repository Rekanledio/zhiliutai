import json
from datetime import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from sqlalchemy import func, select

from app.core.errors import ApplicationError
from app.db.models import (
    ContentVersion,
    KnowledgeItem,
    NoteBinding,
    ProcessingJob,
    SourceArtifact,
)
from app.obsidian.state import watcher_state
from app.schemas.health import DashboardResponse, DashboardStats, HealthResponse
from app.schemas.stage2 import (
    ItemPatchRequest,
    ItemResponse,
    JobResponse,
    ObsidianOpenResponse,
    ObsidianStatusResponse,
    RescanResponse,
    ReviewRequest,
    SubmissionResponse,
    TextSourceRequest,
    UrlSourceRequest,
)
from app.services.health import build_health_report
from app.services.stage2 import Stage2Service

router = APIRouter(prefix="/api")


def service(request: Request) -> Stage2Service:
    return request.app.state.stage2_service


def job_out(job: ProcessingJob) -> JobResponse:
    return JobResponse(
        id=job.id,
        kind=job.kind,
        state=job.state,
        stage=job.stage,
        progress=job.progress,
        retry_count=job.retry_count,
        max_retries=job.max_retries,
        error=json.loads(job.error_json) if job.error_json else None,
        result=json.loads(job.result_json) if job.result_json else None,
        heartbeat_at=job.heartbeat_at,
        created_at=job.created_at,
    )


def item_out(
    item: KnowledgeItem,
    version: ContentVersion | None = None,
    binding: NoteBinding | None = None,
) -> ItemResponse:
    tags: list[str] = []
    source_metadata: dict[str, object] | None = None
    if version is not None:
        parsed = json.loads(version.suggested_tags_json)
        tags = [str(tag) for tag in parsed] if isinstance(parsed, list) else []
        parsed_metadata = json.loads(version.source_metadata_json or "{}")
        if isinstance(parsed_metadata, dict):
            source_metadata = parsed_metadata
    return ItemResponse(
        id=item.id,
        title=item.title,
        source_type=item.source_type,
        status=item.status,
        content_hash=version.content_hash if version else item.content_hash,
        body=version.body if version else None,
        summary=version.summary if version else None,
        suggested_tags=tags,
        source_metadata=source_metadata,
        version_no=version.version_no if version else None,
        note_relative_path=binding.relative_path if binding else None,
        sync_state=binding.sync_state if binding else None,
        created_at=item.created_at,
        updated_at=item.updated_at,
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
        knowledge_count = int(
            (
                await session.execute(
                    select(func.count(KnowledgeItem.id)).where(
                        KnowledgeItem.deleted_at.is_(None)
                    )
                )
            ).scalar_one()
        )
        pending_count = int(
            (
                await session.execute(
                    select(func.count(KnowledgeItem.id)).where(
                        KnowledgeItem.status == "pending_review",
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
                        KnowledgeItem.status == "pending_review",
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
                    .where(KnowledgeItem.deleted_at.is_(None))
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
            today_added=0,
            pending_review=pending_count,
            processing=processing_count,
        ),
        health=await build_health_report(request.app.state.settings),
        pending_reviews=[
            {"id": item.id, "title": item.title, "source_type": item.source_type}
            for item in pending_items
        ],
        recent_items=[
            {"id": item.id, "title": item.title, "status": item.status}
            for item in recent_items
        ],
        processing_jobs=[
            {"id": job.id, "kind": job.kind, "state": job.state, "stage": job.stage}
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
    return [job_out(job) for job in jobs]


@router.get("/jobs/{job_id}", response_model=JobResponse, tags=["jobs"])
async def get_job(job_id: str, request: Request) -> JobResponse:
    async with request.app.state.session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
    if job is None:
        raise ApplicationError(404, "job_not_found", "任务不存在")
    return job_out(job)


@router.post("/jobs/{job_id}/retry", response_model=JobResponse, tags=["jobs"])
async def retry_job(job_id: str, request: Request) -> JobResponse:
    job = await request.app.state.job_runner.retry(job_id)
    return job_out(job)


@router.get("/items", response_model=list[ItemResponse], tags=["items"])
async def list_items(request: Request, status: str | None = None) -> list[ItemResponse]:
    async with request.app.state.session_factory() as session:
        statement = select(KnowledgeItem).where(KnowledgeItem.deleted_at.is_(None))
        if status:
            statement = statement.where(KnowledgeItem.status == status)
        items = list(
            (
                await session.execute(statement.order_by(KnowledgeItem.updated_at.desc()))
            ).scalars()
        )
    return [item_out(item) for item in items]


@router.get("/items/{item_id}", response_model=ItemResponse, tags=["items"])
async def get_item(item_id: str, request: Request) -> ItemResponse:
    item, version, binding = await service(request).get_item(item_id)
    return item_out(item, version, binding)


@router.patch("/items/{item_id}", response_model=ItemResponse, tags=["items"])
async def patch_item(
    item_id: str, payload: ItemPatchRequest, request: Request
) -> ItemResponse:
    item = await service(request).patch_item(
        item_id, payload.title, payload.body, payload.expected_content_hash
    )
    item, version, binding = await service(request).get_item(item.id)
    return item_out(item, version, binding)


@router.post("/items/{item_id}/review", response_model=ItemResponse, tags=["items"])
async def review_item(
    item_id: str, payload: ReviewRequest, request: Request
) -> ItemResponse:
    if not payload.approved:
        raise ApplicationError(422, "review_rejected", "当前只支持明确审核通过")
    await service(request).review(item_id)
    item, version, binding = await service(request).get_item(item_id)
    return item_out(item, version, binding)


@router.post("/items/{item_id}/publish", response_model=ItemResponse, tags=["items"])
async def publish_item(item_id: str, request: Request) -> ItemResponse:
    await service(request).publish(item_id)
    item, version, binding = await service(request).get_item(item_id)
    return item_out(item, version, binding)


@router.post(
    "/items/{item_id}/reprocess", response_model=SubmissionResponse, tags=["items"]
)
async def reprocess_item(item_id: str, request: Request) -> SubmissionResponse:
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


@router.post("/obsidian/rescan", response_model=RescanResponse, tags=["obsidian"])
async def rescan_obsidian(request: Request) -> RescanResponse:
    return RescanResponse(**(await service(request).rescan()))


@router.post(
    "/obsidian/open/{item_id}",
    response_model=ObsidianOpenResponse,
    tags=["obsidian"],
)
async def open_obsidian(item_id: str, request: Request) -> ObsidianOpenResponse:
    item, _version, binding = await service(request).get_item(item_id)
    if binding is None or item.status != "published":
        raise ApplicationError(409, "note_not_published", "条目尚未绑定 Obsidian 笔记")
    return ObsidianOpenResponse(uri=service(request).vault().uri(binding.relative_path))
