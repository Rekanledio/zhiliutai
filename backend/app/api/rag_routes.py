import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import and_, or_, select

from app.core.errors import ApplicationError
from app.core.safety import redact_sensitive_value
from app.db.models import ContentVersion, KnowledgeItem, SourceArtifact
from app.providers.models import ProviderNotConfigured
from app.providers.rag import (
    RagProviderAuthentication,
    RagProviderError,
    RagProviderMalformed,
    RagProviderRateLimited,
    RagProviderTimeout,
    RagProviderUnavailable,
)
from app.rag.citations import CitationBuildError, CitationBuilder
from app.rag.question_answer import (
    AnswerValidationError,
    KnowledgeChangedRetry,
    QuestionAnswerIdempotencyConflict,
    QuestionAnswerService,
)
from app.rag.retrieval import HybridRetriever, RetrievalError
from app.schemas.rag import (
    ChatRequest,
    SearchRequest,
    SearchResponse,
)
from app.services.knowledge import KnowledgeApplicationService
from app.video.subtitles import SubtitleParseError, normalize_subtitle_track
from app.video.types import SubtitleTrack
from app.workflows.question_answer_production import (
    QuestionAnswerWorkflowCoordinator,
    new_question_answer_request_id,
)


router = APIRouter(prefix="/api")


def rag_retriever(request: Request) -> HybridRetriever:
    return request.app.state.rag_retriever


def citation_builder(request: Request) -> CitationBuilder:
    return request.app.state.citation_builder


def knowledge_service(request: Request) -> KnowledgeApplicationService:
    return request.app.state.knowledge_service


def question_answer_service(request: Request) -> QuestionAnswerService:
    return request.app.state.question_answer_service


def question_answer_workflow(request: Request) -> QuestionAnswerWorkflowCoordinator:
    return request.app.state.question_answer_workflow


@router.post(
    "/search",
    response_model=SearchResponse,
    response_model_exclude_none=True,
    tags=["rag"],
)
async def search(payload: SearchRequest, request: Request) -> SearchResponse:
    try:
        return await knowledge_service(request).search(
            payload.query,
            limit=payload.limit,
            source_types=payload.source_types,
        )
    except ValueError as error:
        raise ApplicationError(422, "invalid_search_query", str(error)) from error
    except RetrievalError as error:
        raise ApplicationError(503, "retrieval_unavailable", "检索服务暂不可用") from error
    except CitationBuildError as error:
        raise ApplicationError(409, "knowledge_changed", "知识版本已变化，请重试") from error


def _sse(event: str, payload: object) -> str:
    payload = redact_sensitive_value(payload)
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def _chat_events(result) -> AsyncIterator[str]:
    yield _sse(
        "meta",
        {
            "query": result.query,
            "normalized_query": result.normalized_query,
            "evidence": result.evidence.as_dict(),
            "diagnostics": result.diagnostics.as_dict(),
            "rewrite_query": result.rewrite_query,
            "rewrite_status": result.rewrite_status,
            "refusal": result.refusal,
        },
    )
    if result.refusal:
        yield _sse("delta", {"text": result.refusal, "citation_ids": []})
    else:
        for claim in result.claims:
            yield _sse("delta", claim.as_dict())
    yield _sse(
        "citations",
        {"citations": [citation.as_dict() for citation in result.citations]},
    )
    yield _sse(
        "done",
        {
            "answer": result.answer,
            "conflicts": list(result.conflicts),
            "model_run_id": result.model_run_id,
        },
    )


def _chat_application_error(error: Exception) -> ApplicationError:
    if isinstance(error, QuestionAnswerIdempotencyConflict):
        return ApplicationError(409, "idempotency_conflict", "同一请求 ID 已绑定其他问答请求")
    if isinstance(error, ProviderNotConfigured):
        return ApplicationError(409, "chat_not_configured", "RAG Chat 尚未配置")
    if isinstance(error, KnowledgeChangedRetry):
        return ApplicationError(409, "knowledge_changed", "知识版本已变化，请重试")
    if isinstance(error, AnswerValidationError):
        return ApplicationError(502, "rag_answer_invalid", "答案结构未通过引用校验")
    if isinstance(error, CitationBuildError):
        return ApplicationError(409, "knowledge_changed", "知识版本已变化，请重试")
    if isinstance(error, RagProviderTimeout):
        return ApplicationError(504, "rag_provider_timeout", "RAG Chat 请求超时")
    if isinstance(error, RagProviderRateLimited):
        return ApplicationError(503, "rag_provider_rate_limited", "RAG Chat 服务限流")
    if isinstance(error, RagProviderAuthentication):
        return ApplicationError(502, "rag_provider_authentication", "RAG Chat 鉴权失败")
    if isinstance(error, RagProviderMalformed):
        return ApplicationError(502, "rag_provider_malformed", "RAG Chat 返回结构无效")
    if isinstance(error, RagProviderUnavailable):
        return ApplicationError(503, "rag_provider_unavailable", "RAG Chat 服务暂不可用")
    if isinstance(error, RagProviderError):
        return ApplicationError(502, "rag_provider_error", "RAG Chat 调用失败")
    if isinstance(error, RetrievalError):
        return ApplicationError(503, "retrieval_unavailable", "检索服务暂不可用")
    if isinstance(error, ValueError):
        return ApplicationError(422, "invalid_chat_query", "查询参数无效")
    return ApplicationError(500, "chat_internal_error", "问答服务内部错误")


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    tags=["rag"],
)
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    try:
        request_id = payload.request_id or new_question_answer_request_id()
        result = await question_answer_workflow(request).run(
            {
                "request_id": request_id,
                "safe_query": payload.query,
                "mode": "answer",
            },
            limit=payload.limit,
            rewrite=payload.rewrite,
            source_types=payload.source_types,
        )
    except (
        ProviderNotConfigured,
        KnowledgeChangedRetry,
        AnswerValidationError,
        RagProviderTimeout,
        RagProviderRateLimited,
        RagProviderAuthentication,
        RagProviderMalformed,
        RagProviderUnavailable,
        RagProviderError,
        RetrievalError,
        CitationBuildError,
        ValueError,
    ) as error:
        raise _chat_application_error(error) from error
    return StreamingResponse(
        _chat_events(result),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _source_artifact(artifact_id: str, request: Request) -> SourceArtifact:
    video_artifact_types = {
        "video_source",
        "video_media",
        "video_subtitle",
        "video_transcript",
        "video_keyframe",
    }
    video_media_types = {
        "text/uri-list",
        "text/vtt",
        "text/plain",
        "image/webp",
        "image/png",
        "image/jpeg",
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-matroska",
        "video/mpeg",
        "video/ogg",
    }
    statement = (
        select(SourceArtifact, KnowledgeItem, ContentVersion)
        .join(KnowledgeItem, KnowledgeItem.id == SourceArtifact.knowledge_item_id)
        .join(ContentVersion, ContentVersion.id == KnowledgeItem.current_content_version_id)
        .where(
            SourceArtifact.id == artifact_id,
            ContentVersion.knowledge_item_id == KnowledgeItem.id,
            KnowledgeItem.status == "published",
            KnowledgeItem.deleted_at.is_(None),
            or_(
                and_(
                    SourceArtifact.artifact_type == "original_input",
                    KnowledgeItem.source_type == "pdf",
                    SourceArtifact.media_type == "application/pdf",
                ),
                and_(
                    SourceArtifact.artifact_type == "original_input",
                    KnowledgeItem.source_type == "docx",
                    SourceArtifact.media_type
                    == "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
                and_(
                    KnowledgeItem.source_type == "video",
                    SourceArtifact.artifact_type.in_(video_artifact_types),
                    SourceArtifact.media_type.in_(video_media_types),
                ),
            ),
        )
    )
    async with request.app.state.session_factory() as session:
        row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise ApplicationError(404, "artifact_not_found", "来源 Artifact 不存在")
    artifact, _item, version = row
    try:
        metadata = json.loads(version.source_metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ApplicationError(404, "artifact_not_found", "来源 Artifact 不存在")
    if artifact.artifact_type in {"original_input"}:
        authorized = metadata.get("source_artifact_id") == artifact.id
    elif artifact.artifact_type == "video_source":
        authorized = metadata.get("source_artifact_id") == artifact.id
    elif artifact.artifact_type == "video_media":
        authorized = metadata.get("media_artifact_id") == artifact.id
    elif artifact.artifact_type in {"video_subtitle", "video_transcript"}:
        authorized = artifact.id in {
            metadata.get("subtitle_artifact_id"),
            metadata.get("transcript_artifact_id"),
        }
        manifest = metadata.get("manifest")
        if isinstance(manifest, dict):
            authorized = authorized or artifact.id in {
                manifest.get("transcript_artifact_id"),
                *(
                    manifest.get("subtitle_artifact_ids")
                    if isinstance(manifest.get("subtitle_artifact_ids"), list)
                    else []
                ),
            }
    elif artifact.artifact_type == "video_keyframe":
        authorized = False
        manifest = metadata.get("manifest")
        if isinstance(manifest, dict) and isinstance(manifest.get("keyframes"), list):
            authorized = any(
                isinstance(keyframe, dict) and keyframe.get("artifact_id") == artifact.id
                for keyframe in manifest["keyframes"]
            )
    else:
        authorized = False
    if not authorized:
        raise ApplicationError(404, "artifact_not_found", "来源 Artifact 不存在")
    artifact_store = request.app.state.stage2_service.artifacts
    if not artifact_store.verify(
        artifact.relative_path, artifact.content_hash, artifact.byte_size
    ):
        raise ApplicationError(404, "artifact_not_found", "来源 Artifact 不存在")
    return artifact


def _video_duration(metadata: dict[str, object]) -> int | None:
    manifest = metadata.get("manifest")
    if not isinstance(manifest, dict):
        return None
    source = manifest.get("source_metadata")
    if not isinstance(source, dict):
        return None
    value = source.get("duration_ms")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@router.get("/artifacts/{artifact_id}/locator", tags=["rag"])
async def locate_video_artifact(
    artifact_id: str,
    request: Request,
    start_ms: int | None = Query(default=None, ge=0),
    end_ms: int | None = Query(default=None, gt=0),
    keyframe_id: str | None = Query(default=None, min_length=1, max_length=200),
) -> dict[str, object]:
    """Return an authorized, local video evidence locator.

    This endpoint intentionally returns transcript text or keyframe metadata,
    not a network-media URL.  The frontend can therefore locate evidence even
    after the original video Artifact has been cleaned up.
    """

    artifact = await _source_artifact(artifact_id, request)
    if artifact.artifact_type not in {"video_transcript", "video_keyframe"}:
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")

    async with request.app.state.session_factory() as session:
        row = (
            await session.execute(
                select(ContentVersion)
                .join(KnowledgeItem, KnowledgeItem.current_content_version_id == ContentVersion.id)
                .where(
                    ContentVersion.knowledge_item_id == artifact.knowledge_item_id,
                    ContentVersion.id == KnowledgeItem.current_content_version_id,
                    KnowledgeItem.status == "published",
                    KnowledgeItem.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
    try:
        metadata = json.loads(row.source_metadata_json or "{}")
    except json.JSONDecodeError as error:
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用") from error
    if not isinstance(metadata, dict):
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
    manifest = metadata.get("manifest")
    if not isinstance(manifest, dict):
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")

    duration_ms = _video_duration(metadata)
    if artifact.artifact_type == "video_keyframe":
        keyframes = manifest.get("keyframes")
        if not isinstance(keyframes, list) or not isinstance(keyframe_id, str):
            raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
        selected = next(
            (
                frame
                for frame in keyframes
                if isinstance(frame, dict)
                and frame.get("keyframe_id") == keyframe_id
                and frame.get("artifact_id") == artifact.id
            ),
            None,
        )
        if selected is None:
            raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
        frame_start = selected.get("start_ms")
        frame_end = selected.get("end_ms")
        if (
            not isinstance(frame_start, int)
            or isinstance(frame_start, bool)
            or not isinstance(frame_end, int)
            or isinstance(frame_end, bool)
            or frame_start < 0
            or frame_end <= frame_start
            or (duration_ms is not None and frame_end > duration_ms)
            or (start_ms is not None and start_ms != frame_start)
            or (end_ms is not None and end_ms != frame_end)
        ):
            raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
        return {
            "kind": "keyframe",
            "artifact_id": artifact.id,
            "keyframe_id": keyframe_id,
            "start_ms": frame_start,
            "end_ms": frame_end,
            "media_type": artifact.media_type,
        }

    if keyframe_id is not None or start_ms is None or end_ms is None or start_ms >= end_ms:
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
    if duration_ms is not None and end_ms > duration_ms:
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
    raw_segments = manifest.get("transcript_segments")
    if not isinstance(raw_segments, list):
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
    try:
        content = request.app.state.stage2_service.artifacts.read_bytes(artifact.relative_path)
        language = metadata.get("transcript_language")
        track = SubtitleTrack(
            content=content,
            format="vtt",
            language=language if isinstance(language, str) else None,
            provider="stored-transcript",
            tool_version="artifact-v1",
        )
        actual_segments = normalize_subtitle_track(
            track,
            duration_ms=duration_ms,
            max_bytes=request.app.state.settings.video_max_subtitle_bytes,
            max_segments=request.app.state.settings.video_max_subtitle_segments,
        )
    except (OSError, ValueError, SubtitleParseError) as error:
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用") from error

    manifest_segments: dict[tuple[int, int], dict[str, object]] = {}
    for segment in raw_segments:
        if not isinstance(segment, dict):
            continue
        segment_start = segment.get("start_ms")
        segment_end = segment.get("end_ms")
        segment_text = segment.get("text")
        if (
            isinstance(segment_start, int)
            and not isinstance(segment_start, bool)
            and isinstance(segment_end, int)
            and not isinstance(segment_end, bool)
            and segment_start >= 0
            and segment_end > segment_start
            and isinstance(segment_text, str)
        ):
            manifest_segments[(segment_start, segment_end)] = segment
    selected_segments = []
    for segment in actual_segments:
        if segment.start_ms < start_ms or segment.end_ms > end_ms:
            continue
        manifest_segment = manifest_segments.get((segment.start_ms, segment.end_ms))
        if (
            manifest_segment is not None
            and manifest_segment.get("text") == segment.text
        ):
            selected_segments.append(segment)
    if not selected_segments:
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
    if len(selected_segments) > 512 or sum(len(segment.text) for segment in selected_segments) > 200_000:
        raise ApplicationError(404, "artifact_locator_not_found", "视频定位不可用")
    return {
        "kind": "transcript",
        "artifact_id": artifact.id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "language": selected_segments[0].language,
        "text": " ".join(segment.text for segment in selected_segments),
        "segments": [
            {
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
                "language": segment.language,
            }
            for segment in selected_segments
        ],
    }


@router.head("/artifacts/{artifact_id}", tags=["rag"])
async def head_source_artifact(artifact_id: str, request: Request) -> Response:
    artifact = await _source_artifact(artifact_id, request)
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(artifact.byte_size),
            "Content-Type": artifact.media_type,
        },
    )


@router.get("/artifacts/{artifact_id}", tags=["rag"])
async def read_source_artifact(artifact_id: str, request: Request) -> Response:
    artifact = await _source_artifact(artifact_id, request)
    try:
        content = request.app.state.stage2_service.artifacts.read_bytes(artifact.relative_path)
    except (OSError, ValueError) as error:
        raise ApplicationError(404, "artifact_not_found", "来源 Artifact 不存在") from error
    return Response(content=content, media_type=artifact.media_type)
