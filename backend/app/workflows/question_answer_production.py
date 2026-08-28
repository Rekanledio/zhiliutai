"""Production adapter for the atomic QuestionAnswerService application boundary."""

from __future__ import annotations

from contextvars import ContextVar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import uuid4

from app.rag.question_answer import AnswerResult, QuestionAnswerService
from app.workflows.contracts import (
    QuestionAnswerAnswerResult,
    QuestionAnswerInput,
    QuestionAnswerMode,
    QuestionAnswerRetrievalResult,
    canonical_uuid,
)
from app.workflows.runtime import WorkflowRuntime, WorkflowRuntimeError


@dataclass(frozen=True, slots=True)
class QuestionAnswerRequestOptions:
    """Request options kept outside graph state and persisted by the app service."""

    limit: int = 6
    rewrite: str = "off"
    source_types: tuple[str, ...] | None = None
    mode: QuestionAnswerMode = "answer"


class ProductionQuestionAnswerWorkflowServices:
    """Adapt one atomic Q&A service call to the deterministic graph protocol.

    The graph's ``retrieve`` node invokes ``QuestionAnswerService.answer`` once.
    The graph's ``answer`` node only projects the already completed result into
    stable identifiers; it never reimplements retrieval, evidence policy,
    citation construction, or provider calls.
    """

    def __init__(self, service: QuestionAnswerService) -> None:
        self.service = service
        self._options: dict[str, QuestionAnswerRequestOptions] = {}
        self._failures: dict[str, Exception] = {}
        self._current_options: ContextVar[
            tuple[str, QuestionAnswerRequestOptions] | None
        ] = ContextVar("question_answer_options", default=None)

    def configure(
        self,
        request_id: str,
        *,
        limit: int,
        rewrite: str,
        source_types: Sequence[str] | None,
        mode: QuestionAnswerMode = "answer",
    ) -> None:
        selected_id = canonical_uuid(request_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("检索条数必须在 1 到 100 之间")
        if rewrite not in {"auto", "off"}:
            raise ValueError("rewrite must be auto or off")
        if mode not in {"answer", "search"}:
            raise ValueError("问答模式无效")
        selected_types: tuple[str, ...] | None = None
        if source_types is not None:
            selected_types = tuple(sorted(set(source_types)))
        options = QuestionAnswerRequestOptions(
            limit=limit,
            rewrite=rewrite,
            source_types=selected_types,
            mode=mode,
        )
        self._options[selected_id] = options
        self._current_options.set((selected_id, options))

    def _request_options(self, request_id: str) -> QuestionAnswerRequestOptions:
        selected_id = canonical_uuid(request_id)
        contextual = self._current_options.get()
        if contextual is not None and contextual[0] == selected_id:
            return contextual[1]
        return self._options.get(selected_id, QuestionAnswerRequestOptions())

    async def _execute(
        self,
        *,
        request_id: str,
        safe_query: str,
        mode: QuestionAnswerMode | None = None,
    ) -> AnswerResult:
        selected_id = canonical_uuid(request_id)
        options = self._request_options(selected_id)
        selected_mode = options.mode if mode is None else mode
        if selected_mode != options.mode:
            raise ValueError("问答模式无效")
        try:
            result = await self.service.answer(
                safe_query,
                limit=options.limit,
                rewrite=options.rewrite,
                source_types=options.source_types,
                workflow_request_id=selected_id,
                workflow_mode=selected_mode,
            )
        except Exception as error:
            # This is transient process memory only.  The graph receives only
            # a stable error route, and the exception is mapped before API use.
            self._failures[selected_id] = error
            raise
        return result

    async def ensure_request(
        self,
        *,
        request_id: str,
        safe_query: str,
        mode: QuestionAnswerMode,
        limit: int,
        rewrite: str,
        source_types: Sequence[str] | None,
    ) -> None:
        await self.service.ensure_workflow_request(
            request_id,
            safe_query,
            mode=mode,
            limit=limit,
            rewrite=rewrite,
            source_types=source_types,
        )

    async def retrieve(
        self,
        *,
        request_id: str,
        safe_query: str,
        normalized_query: str,
        mode: QuestionAnswerMode,
    ) -> QuestionAnswerRetrievalResult:
        if mode not in {"answer", "search"}:
            raise ValueError("问答模式无效")
        result = await self._execute(
            request_id=request_id,
            safe_query=safe_query,
            mode=mode,
        )
        return QuestionAnswerRetrievalResult(
            evidence_status=result.evidence.status,
            citation_ids=[citation.citation_id for citation in result.citations],
        )

    async def answer(
        self,
        *,
        request_id: str,
        safe_query: str,
        normalized_query: str,
        citation_ids: list[str],
    ) -> QuestionAnswerAnswerResult:
        result = await self._execute(request_id=request_id, safe_query=safe_query)
        if result.evidence.status != "sufficient" or not result.model_run_id:
            raise ValueError("证据不足时不能生成答案")
        actual_citation_ids = [citation.citation_id for citation in result.citations]
        if actual_citation_ids != citation_ids:
            raise ValueError("问答引用状态不一致")
        return QuestionAnswerAnswerResult(
            model_run_id=result.model_run_id,
            citation_ids=actual_citation_ids,
        )

    def pop_failure(self, request_id: str) -> Exception | None:
        return self._failures.pop(canonical_uuid(request_id), None)

    async def result_for(self, request_id: str, safe_query: str) -> AnswerResult:
        return await self._execute(request_id=request_id, safe_query=safe_query)

    def clear_transient(self) -> None:
        """Drop only in-memory result/error caches; durable request data remains."""

        self._failures.clear()


class QuestionAnswerWorkflowCoordinator:
    """Run the Q&A graph and return the application service's safe rich result."""

    def __init__(
        self,
        runtime: WorkflowRuntime,
        services: ProductionQuestionAnswerWorkflowServices,
    ) -> None:
        self.runtime = runtime
        self.services = services

    async def run(
        self,
        payload: QuestionAnswerInput | Mapping[str, object],
        *,
        limit: int = 6,
        rewrite: str = "off",
        source_types: Sequence[str] | None = None,
    ) -> AnswerResult:
        input_model = (
            payload
            if isinstance(payload, QuestionAnswerInput)
            else QuestionAnswerInput.model_validate(payload)
        )
        self.services.configure(
            input_model.request_id,
            limit=limit,
            rewrite=rewrite,
            source_types=source_types,
            mode=input_model.mode,
        )
        await self.services.ensure_request(
            request_id=input_model.request_id,
            safe_query=input_model.safe_query,
            mode=input_model.mode,
            limit=limit,
            rewrite=rewrite,
            source_types=source_types,
        )
        run = await self.runtime.run_question_answer(
            input_model,
            thread_id=input_model.request_id,
        )
        failure = self.services.pop_failure(input_model.request_id)
        if failure is not None:
            raise failure
        if run.get("route") == "failed":
            raise WorkflowRuntimeError("问答工作流失败")
        return await self.services.result_for(input_model.request_id, input_model.safe_query)


def new_question_answer_request_id() -> str:
    """Generate a system-owned request identity for an API call."""

    return str(uuid4())
