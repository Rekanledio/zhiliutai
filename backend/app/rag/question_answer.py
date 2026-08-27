from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Citation, ModelRun
from app.providers.models import ProviderNotConfigured
from app.providers.rag import (
    AnswerClaim,
    AnswerDraft,
    RagChatProvider,
    RagProviderError,
    RagProviderMalformed,
    RagProviderTimeout,
)
from app.rag.citations import BuiltCitation, CitationBuilder
from app.rag.retrieval import (
    EvidenceAssessment,
    HybridRetriever,
)
from app.rag.types import RetrievalDiagnostics, RetrievedChunk


class AnswerValidationError(RagProviderError):
    pass


class KnowledgeChangedRetry(RagProviderError):
    pass


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
            "query": self.query,
            "normalized_query": self.normalized_query,
            "answer": self.answer,
            "claims": [claim.as_dict() for claim in self.claims],
            "conflicts": list(self.conflicts),
            "citations": [citation.as_dict() for citation in self.citations],
            "evidence": self.evidence.as_dict(),
            "diagnostics": self.diagnostics.as_dict(),
            "rewrite_query": self.rewrite_query,
            "rewrite_status": self.rewrite_status,
            "model_run_id": self.model_run_id,
            "refusal": self.refusal,
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
    ) -> None:
        self.session_factory = session_factory
        self.retriever = retriever
        self.citation_builder = citation_builder
        self.chat_provider = chat_provider

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
            rewritten = await self.chat_provider.rewrite_query(query)
            normalized = self.retriever.query_processor.normalize(rewritten)
        except Exception:
            return None, "fallback"
        if normalized == query:
            return normalized, "unchanged"
        return normalized, "applied"

    async def _start_model_run(
        self,
        *,
        input_payload: dict[str, object],
        parameters: dict[str, object],
    ) -> str:
        provider = self.chat_provider
        assert provider is not None
        run = ModelRun(
            provider=str(getattr(provider, "provider", "rag-chat")),
            model=str(getattr(provider, "model", "unknown")),
            operation="rag_answer",
            prompt_version=str(getattr(provider, "prompt_version", "stage4-rag-v1")),
            parameters_json=json.dumps(parameters, ensure_ascii=False, sort_keys=True),
            input_json=json.dumps(input_payload, ensure_ascii=False, sort_keys=True),
            status="running",
        )
        async with self.session_factory() as session, session.begin():
            session.add(run)
            await session.flush()
            return run.id

    async def _finish_failed(self, run_id: str, error_code: str) -> None:
        async with self.session_factory() as session, session.begin():
            run = await session.get(ModelRun, run_id)
            if run is not None:
                run.status = "failed"
                run.error_json = json.dumps({"code": error_code}, ensure_ascii=False)
                run.latency_ms = None

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
        draft: AnswerDraft,
        citations: Sequence[BuiltCitation],
        input_payload: dict[str, object],
        parameters: dict[str, object],
        started_at: float,
    ) -> None:
        usage = draft.usage
        input_json = json.dumps(input_payload, ensure_ascii=False, sort_keys=True)
        output_json = json.dumps(draft.as_dict(), ensure_ascii=False, sort_keys=True)
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        if input_tokens is None or input_tokens < 0:
            input_tokens = _estimate_tokens(input_json)
        if output_tokens is None or output_tokens < 0:
            output_tokens = _estimate_tokens(
                "\n".join(claim.text for claim in draft.claims)
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
                            {
                                "locator": citation.locator,
                                "target": citation.target,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        retrieval_json=json.dumps(
                            citation.retrieval,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                )

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
                "title": chunk.item_title,
                "content": chunk.content,
                "source_type": chunk.source_type,
            }
            for chunk, citation in zip(chunks, citations, strict=False)
        ]

    async def answer(
        self,
        query: str,
        *,
        limit: int = 6,
        rewrite: str = "off",
        source_types: Sequence[str] | None = None,
    ) -> AnswerResult:
        if rewrite not in {"auto", "off"}:
            raise ValueError("rewrite must be auto or off")
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
                return AnswerResult(
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
                    refusal="证据不足，无法根据当前知识库回答。",
                )
            citations = tuple(await self.citation_builder.build(chunks))
            if not citations or not await self.retriever.validate_current(chunks):
                if attempt == 0:
                    continue
                raise KnowledgeChangedRetry("知识版本在回答期间发生变化，请重试")
            if self.chat_provider is None:
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
            )
            started_at = time.perf_counter()
            try:
                raw_draft = await provider.answer(retrieval_query, evidence)
                draft = self.validate_answer_draft(
                    self._coerce_draft(raw_draft),
                    {citation.citation_id for citation in citations},
                )
            except TimeoutError as error:
                safe_error = RagProviderTimeout("RAG Chat 请求超时")
                await self._finish_failed(
                    run_id, self._provider_error_code(safe_error)
                )
                raise safe_error from error
            except RagProviderError as error:
                await self._finish_failed(run_id, self._provider_error_code(error))
                raise
            except Exception as error:
                safe_error = RagProviderError("RAG Chat 调用失败")
                await self._finish_failed(run_id, self._provider_error_code(safe_error))
                raise safe_error from error
            if not await self.retriever.validate_current(chunks):
                await self._finish_failed(run_id, "knowledge_changed")
                if attempt == 0:
                    continue
                raise KnowledgeChangedRetry("知识版本在回答期间发生变化，请重试")
            await self._finish_success(
                run_id=run_id,
                draft=draft,
                citations=citations,
                input_payload=input_payload,
                parameters=parameters,
                started_at=started_at,
            )
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

    answer_question = answer
