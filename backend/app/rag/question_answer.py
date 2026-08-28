from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.safety import redact_sensitive_text, redact_sensitive_value
from app.db.models import (
    Chunk,
    Citation,
    ContentVersion,
    KnowledgeItem,
    ModelRun,
    WorkflowRequest,
)
from app.providers.models import ProviderNotConfigured
from app.providers.rag import (
    AnswerClaim,
    AnswerDraft,
    RagChatProvider,
    RagProviderError,
    RagProviderMalformed,
    RagProviderTimeout,
)
from app.rag.citations import BuiltCitation, CitationBuildError, CitationBuilder
from app.rag.retrieval import (
    EvidenceAssessment,
    HybridRetriever,
)
from app.rag.types import RetrievalDiagnostics, RetrievedChunk
from app.services.content import content_hash
from app.workflows.contracts import canonical_uuid, sanitize_query


class AnswerValidationError(RagProviderError):
    pass


class KnowledgeChangedRetry(RagProviderError):
    pass


class QuestionAnswerIdempotencyConflict(RagProviderError):
    """The supplied request identity was already claimed for another request."""


WORKFLOW_OPERATION = "rag_answer"
WORKFLOW_FINGERPRINT_VERSION = 1
REFUSAL_TEXT = "证据不足，无法根据当前知识库回答。"


@dataclass(frozen=True)
class AnswerResult:
    query: str
    normalized_query: str
    answer: str | None
    claims: tuple[AnswerClaim, ...]
    conflicts: tuple[str, ...]
    citations: tuple[BuiltCitation, ...]
    evidence: EvidenceAssessment
    diagnostics: RetrievalDiagnostics
    rewrite_query: str | None
    rewrite_status: str
    model_run_id: str | None
    refusal: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": redact_sensitive_text(self.query),
            "normalized_query": redact_sensitive_text(self.normalized_query),
            "answer": redact_sensitive_text(self.answer) if self.answer else None,
            "claims": [
                {
                    "text": redact_sensitive_text(claim.text),
                    "citation_ids": list(claim.citation_ids),
                }
                for claim in self.claims
            ],
            "conflicts": [redact_sensitive_text(conflict) for conflict in self.conflicts],
            "citations": [citation.as_dict() for citation in self.citations],
            "evidence": self.evidence.as_dict(),
            "diagnostics": self.diagnostics.as_dict(),
            "rewrite_query": (
                redact_sensitive_text(self.rewrite_query) if self.rewrite_query else None
            ),
            "rewrite_status": self.rewrite_status,
            "model_run_id": self.model_run_id,
            "refusal": redact_sensitive_text(self.refusal) if self.refusal else None,
        }


def _estimate_tokens(value: str) -> int:
    return max(1, len(value) // 4) if value else 0


def _natural_language_question(query: str) -> bool:
    return query.endswith(("?", "？")) or bool(
        re.search(r"(什么|如何|为什么|是否|哪些|怎么|哪里|请问)", query)
    )


class QuestionAnswerService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        retriever: HybridRetriever,
        citation_builder: CitationBuilder,
        chat_provider: RagChatProvider | None,
        mutation_lock: asyncio.Lock | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.retriever = retriever
        self.citation_builder = citation_builder
        self.chat_provider = chat_provider
        self.mutation_lock = mutation_lock or asyncio.Lock()

    @staticmethod
    def _safe_draft(draft: AnswerDraft) -> AnswerDraft:
        return AnswerDraft(
            claims=tuple(
                AnswerClaim(redact_sensitive_text(claim.text), claim.citation_ids)
                for claim in draft.claims
            ),
            conflicts=tuple(redact_sensitive_text(conflict) for conflict in draft.conflicts),
            usage=draft.usage,
        )

    @staticmethod
    def validate_answer_draft(
        draft: AnswerDraft,
        allowed_citation_ids: set[str],
    ) -> AnswerDraft:
        if not isinstance(draft, AnswerDraft):
            raise AnswerValidationError("答案结构无效")
        if not draft.claims:
            raise AnswerValidationError("答案没有事实 claim")
        claims: list[AnswerClaim] = []
        for claim in draft.claims:
            text = claim.text.strip()
            citation_ids = tuple(dict.fromkeys(identifier.strip() for identifier in claim.citation_ids))
            if not text or not citation_ids:
                raise AnswerValidationError("每个事实 claim 都必须有引用")
            if any(identifier not in allowed_citation_ids for identifier in citation_ids):
                raise AnswerValidationError("答案包含未知 citation ID")
            claims.append(AnswerClaim(text=text, citation_ids=citation_ids))
        return AnswerDraft(
            claims=tuple(claims),
            conflicts=tuple(conflict.strip() for conflict in draft.conflicts if conflict.strip()),
            usage=draft.usage,
        )

    @staticmethod
    def _coerce_draft(value: object) -> AnswerDraft:
        if isinstance(value, AnswerDraft):
            return value
        if not isinstance(value, Mapping):
            raise AnswerValidationError("答案结构无效")
        raw_claims = value.get("claims")
        if not isinstance(raw_claims, list):
            raise AnswerValidationError("答案缺少 claims")
        claims: list[AnswerClaim] = []
        for raw_claim in raw_claims:
            if not isinstance(raw_claim, Mapping):
                raise AnswerValidationError("答案 claim 结构无效")
            raw_ids = raw_claim.get("citation_ids")
            claims.append(
                AnswerClaim(
                    text=str(raw_claim.get("text") or ""),
                    citation_ids=tuple(
                        identifier for identifier in raw_ids if isinstance(identifier, str)
                    )
                    if isinstance(raw_ids, list)
                    else (),
                )
            )
        raw_conflicts = value.get("conflicts", [])
        conflicts = (
            tuple(conflict for conflict in raw_conflicts if isinstance(conflict, str))
            if isinstance(raw_conflicts, list)
            else ()
        )
        return AnswerDraft(claims=tuple(claims), conflicts=conflicts)

    async def _try_rewrite(self, query: str) -> tuple[str | None, str]:
        if (
            self.chat_provider is None
            or not _natural_language_question(query)
            or not callable(getattr(self.chat_provider, "rewrite_query", None))
        ):
            return None, "unavailable"
        try:
            rewritten = await self.chat_provider.rewrite_query(redact_sensitive_text(query))
            normalized = self.retriever.query_processor.normalize(rewritten)
        except Exception:
            return None, "fallback"
        if normalized == query:
            return normalized, "unchanged"
        return normalized, "applied"

    @staticmethod
    def _workflow_parameters(
        *, limit: int, rewrite: str, source_types: Sequence[str] | None
    ) -> dict[str, object]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("检索条数必须在 1 到 100 之间")
        if rewrite not in {"auto", "off"}:
            raise ValueError("rewrite must be auto or off")
        safe_source_types: list[str] | None = None
        if source_types is not None:
            if len(source_types) > 6:
                raise ValueError("source_types 数量无效")
            safe_source_types = []
            for source_type in source_types:
                if (
                    not isinstance(source_type, str)
                    or not source_type
                    or source_type != source_type.strip()
                    or len(source_type) > 32
                ):
                    raise ValueError("source_types 无效")
                safe_source_types.append(source_type)
            if len(set(safe_source_types)) != len(safe_source_types):
                raise ValueError("source_types 无效")
            safe_source_types.sort()
        return {
            "limit": limit,
            "rewrite": rewrite,
            "source_types": safe_source_types,
        }

    @staticmethod
    def _workflow_identity(
        *,
        safe_query: str,
        mode: str,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        if mode not in {"answer", "search"}:
            raise ValueError("问答模式无效")
        query_hash = hashlib.sha256(safe_query.encode("utf-8")).hexdigest()
        identity = {
            "version": WORKFLOW_FINGERPRINT_VERSION,
            "query_sha256": query_hash,
            "mode": mode,
            "options": parameters,
        }
        identity["fingerprint"] = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return identity

    @classmethod
    def _workflow_request_parameters(
        cls,
        *,
        safe_query: str,
        mode: str,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        return {
            **parameters,
            "fingerprint": cls._workflow_identity(
                safe_query=safe_query,
                mode=mode,
                parameters=parameters,
            ),
        }

    async def _ensure_workflow_request(
        self,
        request_id: str,
        parameters: dict[str, object],
        *,
        safe_query: str,
        mode: str,
    ) -> dict[str, object]:
        stored_parameters = self._workflow_request_parameters(
            safe_query=safe_query,
            mode=mode,
            parameters=parameters,
        )
        async with self.session_factory() as session, session.begin():
            request = await session.get(WorkflowRequest, request_id)
            if request is None:
                request = WorkflowRequest(
                    id=request_id,
                    operation=WORKFLOW_OPERATION,
                    status="running",
                    parameters_json=json.dumps(
                        redact_sensitive_value(stored_parameters),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                session.add(request)
                await session.flush()
            elif request.operation != WORKFLOW_OPERATION:
                raise QuestionAnswerIdempotencyConflict("问答请求幂等身份冲突")
            else:
                try:
                    persisted = json.loads(request.parameters_json)
                except json.JSONDecodeError as error:
                    raise KnowledgeChangedRetry("问答请求状态无效，请重试") from error
                if not isinstance(persisted, dict):
                    raise KnowledgeChangedRetry("问答请求状态无效，请重试")
                self._stored_workflow_parameters(request.parameters_json)
                stored_identity = persisted.get("fingerprint")
                if stored_identity != stored_parameters["fingerprint"]:
                    raise QuestionAnswerIdempotencyConflict("问答请求幂等身份冲突")
            return {
                "id": request.id,
                "status": request.status,
                "model_run_id": request.model_run_id,
                "parameters_json": request.parameters_json,
                "error_code": request.error_code,
            }

    async def ensure_workflow_request(
        self,
        request_id: str,
        safe_query: str,
        *,
        mode: str,
        limit: int,
        rewrite: str,
        source_types: Sequence[str] | None,
    ) -> dict[str, object]:
        """Claim or validate durable request identity before graph checkpointing."""

        selected_id = canonical_uuid(request_id)
        safe_query = sanitize_query(safe_query)
        parameters = self._workflow_parameters(
            limit=limit,
            rewrite=rewrite,
            source_types=source_types,
        )
        async with self.mutation_lock:
            return await self._ensure_workflow_request(
                selected_id,
                parameters,
                safe_query=safe_query,
                mode=mode,
            )

    @staticmethod
    def _stored_workflow_parameters(value: object) -> dict[str, object]:
        if not isinstance(value, str):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise KnowledgeChangedRetry("问答请求状态无效，请重试") from error
        if not isinstance(parsed, dict):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        fingerprint = parsed.get("fingerprint")
        if not isinstance(fingerprint, dict):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        if fingerprint.get("version") != WORKFLOW_FINGERPRINT_VERSION:
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        if (
            not isinstance(fingerprint.get("query_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint["query_sha256"])
            or fingerprint.get("mode") not in {"answer", "search"}
        ):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        limit = parsed.get("limit")
        rewrite = parsed.get("rewrite")
        source_types = parsed.get("source_types")
        if source_types is not None and (
            not isinstance(source_types, list)
            or any(not isinstance(item, str) for item in source_types)
        ):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        parameters = QuestionAnswerService._workflow_parameters(
            limit=limit,
            rewrite=rewrite,
            source_types=source_types,
        )
        if fingerprint.get("options") != parameters:
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        identity_without_fingerprint = {
            "version": fingerprint["version"],
            "query_sha256": fingerprint["query_sha256"],
            "mode": fingerprint["mode"],
            "options": fingerprint["options"],
        }
        if hashlib.sha256(
            json.dumps(
                identity_without_fingerprint,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest() != fingerprint.get("fingerprint"):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        return parameters

    async def _mark_workflow_failed(self, request_id: str, error_code: str) -> None:
        async with self.session_factory() as session, session.begin():
            request = await session.get(WorkflowRequest, request_id)
            if request is not None:
                request.status = "failed"
                request.error_code = error_code
                request.result_json = None

    @staticmethod
    def _workflow_snapshot(
        *,
        query: str,
        normalized_query: str,
        assessment: EvidenceAssessment,
        diagnostics: RetrievalDiagnostics,
        rewrite_query: str | None,
        rewrite_status: str,
        refusal_code: str | None = None,
        model_run_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "query": redact_sensitive_text(query),
            "normalized_query": redact_sensitive_text(normalized_query),
            "evidence": redact_sensitive_value(assessment.as_dict()),
            "diagnostics": redact_sensitive_value(diagnostics.as_dict()),
            "rewrite_query": (
                redact_sensitive_text(rewrite_query) if rewrite_query else None
            ),
            "rewrite_status": redact_sensitive_text(rewrite_status),
            "refusal_code": refusal_code,
            "model_run_id": model_run_id,
        }

    async def _mark_workflow_refused(
        self, request_id: str, snapshot: dict[str, object]
    ) -> None:
        async with self.session_factory() as session, session.begin():
            request = await session.get(WorkflowRequest, request_id)
            if request is not None:
                request.status = "refused"
                request.error_code = None
                request.result_json = json.dumps(
                    redact_sensitive_value(snapshot),
                    ensure_ascii=False,
                    sort_keys=True,
                )

    @staticmethod
    def _persisted_assessment(value: object) -> EvidenceAssessment:
        if not isinstance(value, Mapping):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        status = value.get("status")
        reason = value.get("reason")
        if status not in {"none", "low_confidence", "sufficient"} or not isinstance(
            reason, str
        ):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        return EvidenceAssessment(status, redact_sensitive_text(reason))

    @staticmethod
    def _persisted_diagnostics(value: object) -> RetrievalDiagnostics:
        if not isinstance(value, Mapping):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        required_strings = ("original_query", "normalized_query")
        if any(not isinstance(value.get(key), str) for key in required_strings):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        fts_query = value.get("fts_query")
        if fts_query is not None and not isinstance(fts_query, str):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        channel_errors = value.get("channel_errors", {})
        if not isinstance(channel_errors, Mapping) or any(
            not isinstance(key, str) or not isinstance(error, str)
            for key, error in channel_errors.items()
        ):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        booleans = ("fts_available", "vector_available", "degraded", "reranker_available")
        if any(not isinstance(value.get(key), bool) for key in booleans):
            raise KnowledgeChangedRetry("问答请求状态无效，请重试")
        return RetrievalDiagnostics(
            original_query=redact_sensitive_text(value["original_query"]),
            normalized_query=redact_sensitive_text(value["normalized_query"]),
            fts_query=redact_sensitive_text(fts_query) if fts_query else None,
            fts_available=value["fts_available"],
            vector_available=value["vector_available"],
            degraded=value["degraded"],
            channel_errors={
                redact_sensitive_text(key): redact_sensitive_text(error)
                for key, error in channel_errors.items()
            },
            reranker_available=value["reranker_available"],
        )

    async def _load_persisted_citations(
        self, session: AsyncSession, run_id: str
    ) -> tuple[BuiltCitation, ...]:
        rows = (
            await session.execute(
                select(Citation, Chunk, ContentVersion, KnowledgeItem)
                .join(Chunk, Chunk.id == Citation.chunk_id)
                .join(ContentVersion, ContentVersion.id == Chunk.content_version_id)
                .join(KnowledgeItem, KnowledgeItem.id == Chunk.knowledge_item_id)
                .where(
                    Citation.model_run_id == run_id,
                    Citation.knowledge_item_id == KnowledgeItem.id,
                    Citation.content_version_id == ContentVersion.id,
                    Citation.chunk_id == Chunk.id,
                    KnowledgeItem.status == "published",
                    KnowledgeItem.deleted_at.is_(None),
                    KnowledgeItem.current_content_version_id == ContentVersion.id,
                    KnowledgeItem.current_content_version_id == Chunk.content_version_id,
                )
                .order_by(Citation.ordinal.asc(), Citation.id.asc())
            )
        ).all()
        citation_ids = list(
            (
                await session.execute(
                    select(Citation.id).where(Citation.model_run_id == run_id)
                )
            ).scalars()
        )
        if len(rows) != len(citation_ids):
            raise KnowledgeChangedRetry("知识版本在回答恢复期间发生变化，请重试")
        restored: list[BuiltCitation] = []
        for citation, chunk, version, item in rows:
            try:
                actual_chunk_hash = content_hash(chunk.content)
                actual_version_hash = content_hash(version.body)
            except ValueError as error:
                raise KnowledgeChangedRetry(
                    "知识版本在回答恢复期间发生变化，请重试"
                ) from error
            if (
                citation.knowledge_item_id != item.id
                or citation.content_version_id != version.id
                or citation.chunk_id != chunk.id
                or item.content_hash != version.content_hash
                or version.content_hash != actual_version_hash
                or chunk.content_hash != actual_chunk_hash
                or citation.chunk_content_hash != actual_chunk_hash
                or chunk.source_type != item.source_type
            ):
                raise KnowledgeChangedRetry("知识版本在回答恢复期间发生变化，请重试")
            try:
                locator_payload = json.loads(citation.source_locator or "{}")
                retrieval = json.loads(citation.retrieval_json or "{}")
            except json.JSONDecodeError as error:
                raise KnowledgeChangedRetry(
                    "问答引用状态无效，请重试"
                ) from error
            if not isinstance(locator_payload, dict) or not isinstance(retrieval, dict):
                raise KnowledgeChangedRetry("问答引用状态无效，请重试")
            locator = locator_payload.get("locator")
            target = locator_payload.get("target")
            status = locator_payload.get("locator_status", "fallback")
            if (
                not isinstance(locator, dict)
                or not isinstance(target, dict)
                or status not in {"exact", "fallback", "unavailable"}
            ):
                raise KnowledgeChangedRetry("问答引用状态无效，请重试")
            restored.append(
                BuiltCitation(
                    citation_id=citation.label,
                    chunk_id=chunk.id,
                    knowledge_item_id=item.id,
                    content_version_id=version.id,
                    item_title=redact_sensitive_text(version.title),
                    version_no=version.version_no,
                    source_type=item.source_type,
                    excerpt=redact_sensitive_text(citation.excerpt),
                    chunk_content_hash=actual_chunk_hash,
                    locator_status=status,
                    locator=redact_sensitive_value(locator),
                    target=redact_sensitive_value(target),
                    retrieval=redact_sensitive_value(retrieval),
                )
            )
        return tuple(restored)

    async def _load_workflow_result(self, request_id: str) -> AnswerResult | None:
        async with self.session_factory() as session:
            request = await session.get(WorkflowRequest, request_id)
            if request is None or request.status not in {"succeeded", "refused"}:
                return None
            try:
                snapshot = json.loads(request.result_json or "{}")
            except json.JSONDecodeError as error:
                raise KnowledgeChangedRetry("问答请求状态无效，请重试") from error
            if not isinstance(snapshot, dict):
                raise KnowledgeChangedRetry("问答请求状态无效，请重试")
            query = snapshot.get("query")
            normalized_query = snapshot.get("normalized_query")
            if not isinstance(query, str) or not isinstance(normalized_query, str):
                raise KnowledgeChangedRetry("问答请求状态无效，请重试")
            assessment = self._persisted_assessment(snapshot.get("evidence"))
            diagnostics = self._persisted_diagnostics(snapshot.get("diagnostics"))
            rewrite_query = snapshot.get("rewrite_query")
            if rewrite_query is not None and not isinstance(rewrite_query, str):
                raise KnowledgeChangedRetry("问答请求状态无效，请重试")
            rewrite_status = snapshot.get("rewrite_status")
            if not isinstance(rewrite_status, str):
                raise KnowledgeChangedRetry("问答请求状态无效，请重试")
            if request.status == "refused":
                if snapshot.get("refusal_code") != "insufficient_evidence":
                    raise KnowledgeChangedRetry("问答请求状态无效，请重试")
                return AnswerResult(
                    query=query,
                    normalized_query=normalized_query,
                    answer=None,
                    claims=(),
                    conflicts=(),
                    citations=(),
                    evidence=assessment,
                    diagnostics=diagnostics,
                    rewrite_query=rewrite_query,
                    rewrite_status=rewrite_status,
                    model_run_id=None,
                    refusal=REFUSAL_TEXT,
                )
            run_id = request.model_run_id
            if not isinstance(run_id, str):
                raise KnowledgeChangedRetry("问答请求状态无效，请重试")
            run = await session.get(ModelRun, run_id)
            if run is None or run.status != "succeeded" or not run.output_json:
                raise KnowledgeChangedRetry("问答请求状态无效，请重试")
            try:
                draft = self._coerce_draft(json.loads(run.output_json))
            except (json.JSONDecodeError, AnswerValidationError) as error:
                raise KnowledgeChangedRetry("问答请求状态无效，请重试") from error
            citations = await self._load_persisted_citations(session, run.id)
            draft = self.validate_answer_draft(
                draft, {citation.citation_id for citation in citations}
            )
            return AnswerResult(
                query=query,
                normalized_query=normalized_query,
                answer="\n\n".join(claim.text for claim in draft.claims),
                claims=draft.claims,
                conflicts=draft.conflicts,
                citations=citations,
                evidence=assessment,
                diagnostics=diagnostics,
                rewrite_query=rewrite_query,
                rewrite_status=rewrite_status,
                model_run_id=run.id,
            )

    async def _start_model_run(
        self,
        *,
        input_payload: dict[str, object],
        parameters: dict[str, object],
        workflow_request_id: str | None = None,
    ) -> str:
        provider = self.chat_provider
        assert provider is not None
        safe_input = redact_sensitive_value(input_payload)
        safe_parameters = redact_sensitive_value(parameters)
        run = ModelRun(
            provider=redact_sensitive_text(str(getattr(provider, "provider", "rag-chat"))),
            model=redact_sensitive_text(str(getattr(provider, "model", "unknown"))),
            operation="rag_answer",
            prompt_version=redact_sensitive_text(
                str(getattr(provider, "prompt_version", "stage4-rag-v1"))
            ),
            parameters_json=json.dumps(safe_parameters, ensure_ascii=False, sort_keys=True),
            input_json=json.dumps(safe_input, ensure_ascii=False, sort_keys=True),
            status="running",
        )
        async with self.session_factory() as session, session.begin():
            if workflow_request_id is not None:
                workflow_request = await session.get(WorkflowRequest, workflow_request_id)
                if workflow_request is None:
                    raise KnowledgeChangedRetry("问答请求状态无效，请重试")
                if workflow_request.model_run_id is not None:
                    existing = await session.get(ModelRun, workflow_request.model_run_id)
                    if existing is None:
                        raise KnowledgeChangedRetry("问答请求状态无效，请重试")
                    return existing.id
            session.add(run)
            await session.flush()
            if workflow_request_id is not None:
                workflow_request.model_run_id = run.id
            return run.id

    async def _finish_failed(
        self,
        run_id: str,
        error_code: str,
        workflow_request_id: str | None = None,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            run = await session.get(ModelRun, run_id)
            if run is not None:
                run.status = "failed"
                run.error_json = json.dumps({"code": error_code}, ensure_ascii=False)
                run.latency_ms = None
            if workflow_request_id is not None:
                request = await session.get(WorkflowRequest, workflow_request_id)
                if request is not None:
                    request.status = "failed"
                    request.error_code = error_code
                    request.result_json = None

    @staticmethod
    def _evidence_input(citations: Sequence[BuiltCitation]) -> list[dict[str, object]]:
        return [
            {
                "citation_id": citation.citation_id,
                "chunk_id": citation.chunk_id,
                "content_version_id": citation.content_version_id,
                "excerpt": citation.excerpt,
            }
            for citation in citations
        ]

    @staticmethod
    def _provider_error_code(error: BaseException) -> str:
        if isinstance(error, RagProviderTimeout):
            return "rag_provider_timeout"
        if isinstance(error, RagProviderMalformed):
            return "rag_provider_malformed"
        if isinstance(error, AnswerValidationError):
            return "rag_answer_invalid"
        if isinstance(error, ProviderNotConfigured):
            return "rag_provider_not_configured"
        if isinstance(error, RagProviderError):
            return "rag_provider_error"
        return "rag_provider_error"

    async def _finish_success(
        self,
        *,
        run_id: str,
        chunks: Sequence[RetrievedChunk],
        draft: AnswerDraft,
        citations: Sequence[BuiltCitation],
        input_payload: dict[str, object],
        parameters: dict[str, object],
        started_at: float,
        workflow_request_id: str | None = None,
        workflow_snapshot: dict[str, object] | None = None,
    ) -> bool:
        usage = draft.usage
        safe_input = redact_sensitive_value(input_payload)
        safe_draft = self._safe_draft(draft)
        input_json = json.dumps(safe_input, ensure_ascii=False, sort_keys=True)
        output_json = json.dumps(safe_draft.as_dict(), ensure_ascii=False, sort_keys=True)
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        if input_tokens is None or input_tokens < 0:
            input_tokens = _estimate_tokens(input_json)
        if output_tokens is None or output_tokens < 0:
            output_tokens = _estimate_tokens(
                "\n".join(claim.text for claim in safe_draft.claims)
            )
        stored_parameters = {
            **parameters,
            "usage_estimated": bool(
                usage.estimated
                or usage.input_tokens is None
                or usage.output_tokens is None
            ),
        }
        async with self.session_factory() as session, session.begin():
            run = await session.get(ModelRun, run_id)
            if run is None:
                raise RuntimeError("RAG ModelRun disappeared")
            rows = (
                await session.execute(
                    select(Chunk, ContentVersion, KnowledgeItem)
                    .join(ContentVersion, ContentVersion.id == Chunk.content_version_id)
                    .join(KnowledgeItem, KnowledgeItem.id == Chunk.knowledge_item_id)
                    .where(
                        Chunk.id.in_([chunk.chunk_id for chunk in chunks]),
                        Chunk.knowledge_item_id == KnowledgeItem.id,
                        ContentVersion.knowledge_item_id == KnowledgeItem.id,
                        KnowledgeItem.status == "published",
                        KnowledgeItem.deleted_at.is_(None),
                        KnowledgeItem.current_content_version_id == ContentVersion.id,
                        KnowledgeItem.current_content_version_id == Chunk.content_version_id,
                    )
                )
            ).all()
            current = {chunk.id: (chunk, version, item) for chunk, version, item in rows}
            citations_by_chunk = {citation.chunk_id: citation for citation in citations}
            valid_snapshot = (
                len(current) == len(chunks)
                and len(citations) == len(chunks)
                and len(citations_by_chunk) == len(citations)
            )
            if valid_snapshot:
                for retrieved in chunks:
                    row = current.get(retrieved.chunk_id)
                    citation = citations_by_chunk.get(retrieved.chunk_id)
                    if row is None or citation is None:
                        valid_snapshot = False
                        break
                    chunk, version, item = row
                    try:
                        actual_hash = content_hash(chunk.content)
                        version_hash = content_hash(version.body)
                    except ValueError:
                        valid_snapshot = False
                        break
                    if (
                        citation.knowledge_item_id != item.id
                        or citation.content_version_id != version.id
                        or citation.chunk_content_hash != actual_hash
                        or citation.source_type != item.source_type
                        or chunk.source_type != item.source_type
                        or chunk.content_hash != actual_hash
                        or version.content_hash != version_hash
                        or item.content_hash != version.content_hash
                    ):
                        valid_snapshot = False
                        break
            if not valid_snapshot:
                run.status = "failed"
                run.error_json = json.dumps(
                    {"code": "knowledge_changed"}, ensure_ascii=False
                )
                run.latency_ms = None
                return False
            run.status = "succeeded"
            run.parameters_json = json.dumps(
                stored_parameters, ensure_ascii=False, sort_keys=True
            )
            run.input_json = input_json
            run.output_json = output_json
            run.input_tokens = input_tokens
            run.output_tokens = output_tokens
            run.latency_ms = round((time.perf_counter() - started_at) * 1000, 3)
            run.error_json = None
            for ordinal, citation in enumerate(citations, start=1):
                session.add(
                    Citation(
                        model_run_id=run_id,
                        chunk_id=citation.chunk_id,
                        knowledge_item_id=citation.knowledge_item_id,
                        content_version_id=citation.content_version_id,
                        label=citation.citation_id,
                        ordinal=ordinal,
                        source_type=citation.source_type,
                        excerpt=citation.excerpt,
                        chunk_content_hash=citation.chunk_content_hash,
                        source_locator=json.dumps(
                            redact_sensitive_value(
                                {
                                    "locator_status": citation.locator_status,
                                    "locator": citation.locator,
                                    "target": citation.target,
                                }
                            ),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        retrieval_json=json.dumps(
                            redact_sensitive_value(citation.retrieval),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                )
            if workflow_request_id is not None:
                request = await session.get(WorkflowRequest, workflow_request_id)
                if request is None or workflow_snapshot is None:
                    raise KnowledgeChangedRetry("问答请求状态无效，请重试")
                request.status = "succeeded"
                request.error_code = None
                request.result_json = json.dumps(
                    redact_sensitive_value(
                        {
                            **workflow_snapshot,
                            "model_run_id": run_id,
                        }
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
        return True

    async def _retrieve_answer_context(
        self,
        query: str,
        *,
        limit: int,
        rewrite: str,
        source_types: Sequence[str] | None,
    ) -> tuple[
        str,
        tuple[RetrievedChunk, ...],
        EvidenceAssessment,
        RetrievalDiagnostics,
        str | None,
        str,
    ]:
        chunks_result, diagnostics, assessment = await self.retriever.retrieve(
            query,
            limit=limit,
            source_types=source_types,
        )
        retrieval_query = diagnostics.normalized_query
        chunks = tuple(chunks_result)
        rewrite_query: str | None = None
        rewrite_status = "off"
        if assessment.status != "sufficient":
            return (
                retrieval_query,
                chunks,
                assessment,
                diagnostics,
                rewrite_query,
                "skipped_evidence" if rewrite == "auto" else rewrite_status,
            )
        if rewrite == "auto":
            rewrite_query, rewrite_status = await self._try_rewrite(retrieval_query)
            if rewrite_status == "applied" and rewrite_query:
                (
                    rewritten_chunks,
                    rewritten_diagnostics,
                    rewritten_assessment,
                ) = await self.retriever.retrieve(
                    rewrite_query,
                    limit=limit,
                    source_types=source_types,
                )
                if rewritten_assessment.status == "sufficient":
                    retrieval_query = rewritten_diagnostics.normalized_query
                    chunks = tuple(rewritten_chunks)
                    assessment = rewritten_assessment
                    diagnostics = rewritten_diagnostics
                else:
                    rewrite_status = "fallback"
        return (
            retrieval_query,
            chunks,
            assessment,
            diagnostics,
            rewrite_query,
            rewrite_status,
        )

    @staticmethod
    def _provider_evidence(
        chunks: Sequence[RetrievedChunk],
        citations: Sequence[BuiltCitation],
    ) -> list[dict[str, str]]:
        return [
            {
                "citation_id": citation.citation_id,
                "title": redact_sensitive_text(chunk.item_title),
                "content": redact_sensitive_text(chunk.content),
                "source_type": redact_sensitive_text(chunk.source_type),
            }
            for chunk, citation in zip(chunks, citations, strict=False)
        ]

    async def _answer_impl(
        self,
        query: str,
        *,
        limit: int = 6,
        rewrite: str = "off",
        source_types: Sequence[str] | None = None,
        workflow_request_id: str | None = None,
        workflow_mode: str = "answer",
    ) -> AnswerResult:
        selected_workflow_id = (
            None if workflow_request_id is None else canonical_uuid(workflow_request_id)
        )
        parameters = self._workflow_parameters(
            limit=limit,
            rewrite=rewrite,
            source_types=source_types,
        )
        if selected_workflow_id is not None:
            request = await self._ensure_workflow_request(
                selected_workflow_id,
                parameters,
                safe_query=query,
                mode=workflow_mode,
            )
            if request["status"] in {"succeeded", "refused"}:
                restored = await self._load_workflow_result(selected_workflow_id)
                if restored is not None:
                    return restored
                raise KnowledgeChangedRetry("问答请求状态无效，请重试")
            if request["status"] == "failed":
                raise RagProviderError("问答请求已失败，请重新发起")
            stored_parameters = self._stored_workflow_parameters(
                request["parameters_json"]
            )
            limit = stored_parameters["limit"]
            rewrite = stored_parameters["rewrite"]
            source_types = stored_parameters["source_types"]
        for attempt in range(2):
            (
                retrieval_query,
                chunks,
                assessment,
                diagnostics,
                rewrite_query,
                rewrite_status,
            ) = await self._retrieve_answer_context(
                query,
                limit=limit,
                rewrite=rewrite,
                source_types=source_types,
            )
            if assessment.status != "sufficient":
                result = AnswerResult(
                    query=query,
                    normalized_query=diagnostics.normalized_query,
                    answer=None,
                    claims=(),
                    conflicts=(),
                    citations=(),
                    evidence=assessment,
                    diagnostics=diagnostics,
                    rewrite_query=rewrite_query,
                    rewrite_status=rewrite_status,
                    model_run_id=None,
                    refusal=REFUSAL_TEXT,
                )
                if selected_workflow_id is not None:
                    await self._mark_workflow_refused(
                        selected_workflow_id,
                        self._workflow_snapshot(
                            query=query,
                            normalized_query=diagnostics.normalized_query,
                            assessment=assessment,
                            diagnostics=diagnostics,
                            rewrite_query=rewrite_query,
                            rewrite_status=rewrite_status,
                            refusal_code="insufficient_evidence",
                        ),
                    )
                return result
            try:
                citations = tuple(await self.citation_builder.build(chunks))
            except CitationBuildError as error:
                if attempt == 0:
                    continue
                raise KnowledgeChangedRetry("知识版本在回答期间发生变化，请重试") from error
            if not citations:
                if attempt == 0:
                    continue
                raise KnowledgeChangedRetry("知识版本在回答期间发生变化，请重试")
            if self.chat_provider is None:
                if selected_workflow_id is not None:
                    await self._mark_workflow_failed(
                        selected_workflow_id, "rag_provider_not_configured"
                    )
                raise ProviderNotConfigured("RAG Chat capability is not configured")
            provider = self.chat_provider
            evidence = self._provider_evidence(chunks, citations)
            input_payload = {
                "query": query,
                "retrieval_query": retrieval_query,
                "rewrite": {
                    "requested": rewrite,
                    "status": rewrite_status,
                    "query": rewrite_query,
                },
                "evidence": self._evidence_input(citations),
            }
            parameters: dict[str, object] = {
                "limit": limit,
                "rewrite": rewrite,
                "source_types": list(source_types) if source_types else None,
            }
            run_id = await self._start_model_run(
                input_payload=input_payload,
                parameters=parameters,
                workflow_request_id=selected_workflow_id,
            )
            started_at = time.perf_counter()
            try:
                raw_draft = await provider.answer(
                    redact_sensitive_text(retrieval_query), evidence
                )
                draft = self.validate_answer_draft(
                    self._coerce_draft(raw_draft),
                    {citation.citation_id for citation in citations},
                )
                draft = self._safe_draft(draft)
            except TimeoutError as error:
                safe_error = RagProviderTimeout("RAG Chat 请求超时")
                await self._finish_failed(
                    run_id,
                    self._provider_error_code(safe_error),
                    selected_workflow_id,
                )
                raise safe_error from error
            except RagProviderError as error:
                await self._finish_failed(
                    run_id,
                    self._provider_error_code(error),
                    selected_workflow_id,
                )
                raise
            except Exception as error:
                safe_error = RagProviderError("RAG Chat 调用失败")
                await self._finish_failed(
                    run_id,
                    self._provider_error_code(safe_error),
                    selected_workflow_id,
                )
                raise safe_error from error
            if selected_workflow_id is None:
                async with self.mutation_lock:
                    persisted = await self._finish_success(
                        run_id=run_id,
                        chunks=chunks,
                        draft=draft,
                        citations=citations,
                        input_payload=input_payload,
                        parameters=parameters,
                        started_at=started_at,
                        workflow_request_id=selected_workflow_id,
                        workflow_snapshot=None,
                    )
            else:
                persisted = await self._finish_success(
                    run_id=run_id,
                    chunks=chunks,
                    draft=draft,
                    citations=citations,
                    input_payload=input_payload,
                    parameters=parameters,
                    started_at=started_at,
                    workflow_request_id=selected_workflow_id,
                    workflow_snapshot=(
                        self._workflow_snapshot(
                            query=query,
                            normalized_query=retrieval_query,
                            assessment=assessment,
                            diagnostics=diagnostics,
                            rewrite_query=rewrite_query,
                            rewrite_status=rewrite_status,
                            model_run_id=run_id,
                        )
                        if selected_workflow_id is not None
                        else None
                    ),
                )
            if not persisted:
                if attempt == 0:
                    continue
                raise KnowledgeChangedRetry("知识版本在回答期间发生变化，请重试")
            return AnswerResult(
                query=query,
                normalized_query=retrieval_query,
                answer="\n\n".join(claim.text for claim in draft.claims),
                claims=draft.claims,
                conflicts=draft.conflicts,
                citations=citations,
                evidence=assessment,
                diagnostics=diagnostics,
                rewrite_query=rewrite_query,
                rewrite_status=rewrite_status,
                model_run_id=run_id,
            )
        raise KnowledgeChangedRetry("知识版本在回答期间发生变化，请重试")

    async def answer(
        self,
        query: str,
        *,
        limit: int = 6,
        rewrite: str = "off",
        source_types: Sequence[str] | None = None,
        workflow_request_id: str | None = None,
        workflow_mode: str = "answer",
    ) -> AnswerResult:
        safe_query = sanitize_query(query)
        selected_workflow_id = (
            None if workflow_request_id is None else canonical_uuid(workflow_request_id)
        )
        if selected_workflow_id is None:
            return await self._answer_impl(
                safe_query,
                limit=limit,
                rewrite=rewrite,
                source_types=source_types,
                workflow_request_id=None,
                workflow_mode=workflow_mode,
            )
        async with self.mutation_lock:
            return await self._answer_impl(
                safe_query,
                limit=limit,
                rewrite=rewrite,
                source_types=source_types,
                workflow_request_id=selected_workflow_id,
                workflow_mode=workflow_mode,
            )

    answer_question = answer
