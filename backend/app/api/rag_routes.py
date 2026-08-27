import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import and_, or_, select

from app.core.errors import ApplicationError
from app.db.models import KnowledgeItem, SourceArtifact
from app.providers.models import ProviderNotConfigured
from app.providers.rag import (
    RagProviderAuthentication,
    RagProviderError,
    RagProviderMalformed,
    RagProviderRateLimited,
    RagProviderTimeout,
    RagProviderUnavailable,
)
from app.rag.citations import CitationBuilder
from app.rag.question_answer import (
    AnswerValidationError,
    KnowledgeChangedRetry,
    QuestionAnswerService,
)
from app.rag.retrieval import HybridRetriever, RetrievalError
from app.schemas.rag import (
    ChatRequest,
    CitationResponse,
    EvidenceResponse,
    RetrievalDiagnosticsResponse,
    SearchRequest,
    SearchResponse,
    SearchResult,
)


router = APIRouter(prefix="/api")


def rag_retriever(request: Request) -> HybridRetriever:
    return request.app.state.rag_retriever


def citation_builder(request: Request) -> CitationBuilder:
    return request.app.state.citation_builder


def question_answer_service(request: Request) -> QuestionAnswerService:
    return request.app.state.question_answer_service


@router.post(
    "/search",
    response_model=SearchResponse,
    response_model_exclude_none=True,
    tags=["rag"],
)
async def search(payload: SearchRequest, request: Request) -> SearchResponse:
    try:
        chunks, diagnostics, assessment = await rag_retriever(request).retrieve(
            payload.query,
            limit=payload.limit,
            source_types=payload.source_types,
        )
    except ValueError as error:
        raise ApplicationError(422, "invalid_search_query", str(error)) from error
    except RetrievalError as error:
        raise ApplicationError(503, "retrieval_unavailable", "检索服务暂不可用") from error

    citations = await citation_builder(request).build(chunks)
    results = [
        SearchResult(
            chunk_id=chunk.chunk_id,
            knowledge_item_id=chunk.knowledge_item_id,
            content_version_id=chunk.content_version_id,
            item_title=chunk.item_title,
            version_no=chunk.version_no,
            source_type=chunk.source_type,
            excerpt=citation.excerpt,
            citation=CitationResponse(**citation.as_dict()),
        )
        for chunk, citation in zip(chunks, citations, strict=True)
    ]
    return SearchResponse(
        query=payload.query,
        normalized_query=diagnostics.normalized_query,
        results=results,
        evidence=EvidenceResponse(**assessment.as_dict()),
        diagnostics=RetrievalDiagnosticsResponse(**diagnostics.as_dict()),
        searched_at=datetime.now(timezone.utc),
    )


def _sse(event: str, payload: object) -> str:
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
    if isinstance(error, ProviderNotConfigured):
        return ApplicationError(409, "chat_not_configured", "RAG Chat 尚未配置")
    if isinstance(error, KnowledgeChangedRetry):
        return ApplicationError(409, "knowledge_changed", "知识版本已变化，请重试")
    if isinstance(error, AnswerValidationError):
        return ApplicationError(502, "rag_answer_invalid", "答案结构未通过引用校验")
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
        return ApplicationError(422, "invalid_chat_query", str(error))
    return ApplicationError(500, "chat_internal_error", "问答服务内部错误")


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    tags=["rag"],
)
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    try:
        result = await question_answer_service(request).answer(
            payload.query,
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
        ValueError,
    ) as error:
        raise _chat_application_error(error) from error
    return StreamingResponse(
        _chat_events(result),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/artifacts/{artifact_id}", tags=["rag"])
async def read_source_artifact(artifact_id: str, request: Request) -> Response:
    statement = (
        select(SourceArtifact)
        .join(KnowledgeItem, KnowledgeItem.id == SourceArtifact.knowledge_item_id)
        .where(
            SourceArtifact.id == artifact_id,
            SourceArtifact.artifact_type == "original_input",
            KnowledgeItem.status == "published",
            KnowledgeItem.deleted_at.is_(None),
            or_(
                and_(
                    KnowledgeItem.source_type == "pdf",
                    SourceArtifact.media_type == "application/pdf",
                ),
                and_(
                    KnowledgeItem.source_type == "docx",
                    SourceArtifact.media_type
                    == "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            ),
        )
    )
    async with request.app.state.session_factory() as session:
        artifact = (await session.execute(statement)).scalar_one_or_none()
    if artifact is None:
        raise ApplicationError(404, "artifact_not_found", "来源 Artifact 不存在")
    try:
        content = request.app.state.stage2_service.artifacts.read_bytes(artifact.relative_path)
    except (OSError, ValueError) as error:
        raise ApplicationError(404, "artifact_not_found", "来源 Artifact 不存在") from error
    return Response(content=content, media_type=artifact.media_type)
