"""Safe invocation helpers and lifecycle-aware runtime for stage 6 graphs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.types import Command
from pydantic import ValidationError

from app.core.config import Settings
from app.workflows.checkpoints import WorkflowCheckpoint, workflow_checkpoint_path
from app.workflows.contracts import (
    IngestionInput,
    IngestionInterruptPayload,
    IngestionResumeDecision,
    IngestionStateModel,
    IngestionWorkflowServices,
    QuestionAnswerInput,
    QuestionAnswerStateModel,
    QuestionAnswerWorkflowServices,
    canonical_uuid,
)
from app.workflows.ingestion import build_ingestion_graph
from app.workflows.question_answer import build_question_answer_graph


class WorkflowRuntimeError(RuntimeError):
    """A stable runtime error that never includes user or provider text."""


def new_thread_id() -> str:
    """Generate a system-owned thread identifier."""

    return str(uuid4())


def validate_thread_id(value: object) -> str:
    """Accept only a canonical UUID for the checkpointer's thread key."""

    try:
        return canonical_uuid(value)
    except ValueError as error:
        raise WorkflowRuntimeError("thread_id must be a canonical UUID") from error


def thread_config(thread_id: str) -> dict[str, dict[str, str]]:
    """Build the only config field that carries a thread identity."""

    return {"configurable": {"thread_id": validate_thread_id(thread_id)}}


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """Transient caller-facing result; it is never written to graph state."""

    thread_id: str
    result: dict[str, object]

    @property
    def state(self) -> dict[str, object]:
        return self.result

    @property
    def interrupted(self) -> bool:
        return bool(self.result.get("__interrupt__"))

    def get(self, key: str, default: object = None) -> object:
        return self.result.get(key, default)

    def __getitem__(self, key: str) -> object:
        return self.result[key]


def _safe_state_values(values: object, model_type: type[Any]) -> dict[str, object]:
    if not isinstance(values, Mapping) or any(not isinstance(key, str) for key in values):
        raise WorkflowRuntimeError("checkpoint state is invalid")
    try:
        model = model_type.model_validate(dict(values))
    except ValidationError as error:
        raise WorkflowRuntimeError("checkpoint state is invalid") from error
    return model.model_dump(mode="json")


def _safe_interrupt_values(values: object) -> list[dict[str, object]]:
    if not isinstance(values, (tuple, list)):
        raise WorkflowRuntimeError("interrupt payload is invalid")
    safe_values: list[dict[str, object]] = []
    for value in values:
        payload_value = getattr(value, "value", value)
        try:
            payload = IngestionInterruptPayload.model_validate(payload_value)
        except ValidationError as error:
            raise WorkflowRuntimeError("interrupt payload is invalid") from error
        safe_values.append(payload.model_dump(mode="json"))
    return safe_values


def _safe_graph_result(result: object, model_type: type[Any]) -> dict[str, object]:
    if not isinstance(result, Mapping):
        raise WorkflowRuntimeError("graph result is invalid")
    state_values = {key: value for key, value in result.items() if key != "__interrupt__"}
    safe_result = _safe_state_values(state_values, model_type)
    if "__interrupt__" in result:
        safe_result["__interrupt__"] = _safe_interrupt_values(result["__interrupt__"])
    return safe_result


def _safe_snapshot(snapshot: Any, model_type: type[Any]) -> dict[str, object]:
    safe_result = _safe_state_values(snapshot.values, model_type)
    if snapshot.interrupts:
        safe_result["__interrupt__"] = _safe_interrupt_values(snapshot.interrupts)
    return safe_result


class WorkflowRuntime:
    """Compile injected graphs inside one explicitly owned checkpoint session."""

    def __init__(
        self,
        ingestion_services: IngestionWorkflowServices | None = None,
        question_answer_services: QuestionAnswerWorkflowServices | None = None,
        *,
        checkpoint_path: Path | str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._ingestion_services = ingestion_services
        self._question_answer_services = question_answer_services
        self._settings = settings
        self._checkpoint_path = (
            workflow_checkpoint_path(settings) if checkpoint_path is None else Path(checkpoint_path)
        )
        self._checkpoint: WorkflowCheckpoint | None = None
        self._ingestion_graph = None
        self._question_answer_graph = None

    @property
    def checkpoint(self) -> WorkflowCheckpoint:
        if self._checkpoint is None:
            raise WorkflowRuntimeError("workflow runtime is not open")
        return self._checkpoint

    @property
    def ingestion_graph(self):
        if self._ingestion_graph is None:
            raise WorkflowRuntimeError("ingestion graph is not configured")
        return self._ingestion_graph

    @property
    def question_answer_graph(self):
        if self._question_answer_graph is None:
            raise WorkflowRuntimeError("question-answer graph is not configured")
        return self._question_answer_graph

    async def __aenter__(self) -> WorkflowRuntime:
        if self._checkpoint is not None:
            raise WorkflowRuntimeError("workflow runtime is already open")
        timeout = 5_000 if self._settings is None else self._settings.sqlite_busy_timeout_ms
        checkpoint = WorkflowCheckpoint(self._checkpoint_path, busy_timeout_ms=timeout)
        try:
            await checkpoint.__aenter__()
            self._checkpoint = checkpoint
            if self._ingestion_services is not None:
                self._ingestion_graph = build_ingestion_graph(
                    self._ingestion_services,
                    checkpointer=checkpoint.saver,
                )
            if self._question_answer_services is not None:
                self._question_answer_graph = build_question_answer_graph(
                    self._question_answer_services,
                    checkpointer=checkpoint.saver,
                )
        except BaseException:
            self._checkpoint = None
            self._ingestion_graph = None
            self._question_answer_graph = None
            await checkpoint.aclose()
            raise
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool | None:
        checkpoint = self._checkpoint
        self._checkpoint = None
        self._ingestion_graph = None
        self._question_answer_graph = None
        if checkpoint is None:
            return None
        return await checkpoint.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        await self.__aexit__(None, None, None)

    async def run_ingestion(
        self,
        payload: IngestionInput | Mapping[str, object],
        *,
        thread_id: str | None = None,
    ) -> WorkflowRun:
        input_model = payload if isinstance(payload, IngestionInput) else IngestionInput.model_validate(payload)
        selected_thread_id = new_thread_id() if thread_id is None else validate_thread_id(thread_id)
        result = await self.ingestion_graph.ainvoke(
            input_model.to_state(),
            config=thread_config(selected_thread_id),
        )
        return WorkflowRun(
            selected_thread_id,
            _safe_graph_result(result, IngestionStateModel),
        )

    async def resume_ingestion(
        self,
        thread_id: str,
        decision: IngestionResumeDecision | Mapping[str, object],
    ) -> WorkflowRun:
        selected_thread_id = validate_thread_id(thread_id)
        decision_model = (
            decision
            if isinstance(decision, IngestionResumeDecision)
            else IngestionResumeDecision.model_validate(decision)
        )
        config = thread_config(selected_thread_id)
        snapshot = await self.ingestion_graph.aget_state(config)
        safe_snapshot = _safe_snapshot(snapshot, IngestionStateModel)
        if snapshot.interrupts:
            result = await self.ingestion_graph.ainvoke(
                Command(resume=decision_model.model_dump(mode="json")),
                config=config,
            )
            return WorkflowRun(
                selected_thread_id,
                _safe_graph_result(result, IngestionStateModel),
            )
        if safe_snapshot.get("stage") in {"completed", "rejected", "cancelled", "failed"}:
            return WorkflowRun(selected_thread_id, safe_snapshot)
        raise WorkflowRuntimeError("workflow is not waiting for a decision")

    async def snapshot_ingestion(self, thread_id: str) -> WorkflowRun:
        """Return a validated persisted ingestion state without advancing it."""

        selected_thread_id = validate_thread_id(thread_id)
        snapshot = await self.ingestion_graph.aget_state(thread_config(selected_thread_id))
        return WorkflowRun(
            selected_thread_id,
            _safe_snapshot(snapshot, IngestionStateModel),
        )

    async def continue_ingestion(self, thread_id: str) -> WorkflowRun:
        """Continue a non-interrupted persisted graph after process recovery."""

        selected_thread_id = validate_thread_id(thread_id)
        config = thread_config(selected_thread_id)
        snapshot = await self.ingestion_graph.aget_state(config)
        if snapshot.interrupts:
            raise WorkflowRuntimeError("workflow is waiting for a decision")
        if not snapshot.values and not snapshot.next:
            raise WorkflowRuntimeError("workflow checkpoint is unavailable")
        result = await self.ingestion_graph.ainvoke(None, config=config)
        return WorkflowRun(
            selected_thread_id,
            _safe_graph_result(result, IngestionStateModel),
        )

    async def retry_ingestion(self, thread_id: str) -> WorkflowRun:
        selected_thread_id = validate_thread_id(thread_id)
        config = thread_config(selected_thread_id)
        snapshot = await self.ingestion_graph.aget_state(config)
        safe_snapshot = _safe_snapshot(snapshot, IngestionStateModel)
        if safe_snapshot.get("stage") != "failed":
            return WorkflowRun(selected_thread_id, safe_snapshot)
        if snapshot.parent_config is None:
            raise WorkflowRuntimeError("workflow retry checkpoint is unavailable")
        result = await self.ingestion_graph.ainvoke(None, config=snapshot.parent_config)
        return WorkflowRun(
            selected_thread_id,
            _safe_graph_result(result, IngestionStateModel),
        )

    async def run_question_answer(
        self,
        payload: QuestionAnswerInput | Mapping[str, object],
        *,
        thread_id: str | None = None,
    ) -> WorkflowRun:
        input_model = (
            payload
            if isinstance(payload, QuestionAnswerInput)
            else QuestionAnswerInput.model_validate(payload)
        )
        selected_thread_id = new_thread_id() if thread_id is None else validate_thread_id(thread_id)
        config = thread_config(selected_thread_id)
        existing = await self.question_answer_graph.aget_state(config)
        if existing.values and not existing.interrupts:
            safe_existing = _safe_snapshot(existing, QuestionAnswerStateModel)
            if safe_existing.get("route") in {"completed", "failed"}:
                return WorkflowRun(selected_thread_id, safe_existing)
        result = await self.question_answer_graph.ainvoke(
            input_model.to_state(),
            config=config,
        )
        return WorkflowRun(
            selected_thread_id,
            _safe_graph_result(result, QuestionAnswerStateModel),
        )

    async def snapshot_question_answer(self, thread_id: str) -> WorkflowRun:
        """Return a validated persisted QA state without advancing it."""

        selected_thread_id = validate_thread_id(thread_id)
        snapshot = await self.question_answer_graph.aget_state(
            thread_config(selected_thread_id)
        )
        return WorkflowRun(
            selected_thread_id,
            _safe_snapshot(snapshot, QuestionAnswerStateModel),
        )


@asynccontextmanager
async def open_workflow_runtime(
    ingestion_services: IngestionWorkflowServices | None = None,
    question_answer_services: QuestionAnswerWorkflowServices | None = None,
    *,
    checkpoint_path: Path | str | None = None,
    settings: Settings | None = None,
) -> AsyncIterator[WorkflowRuntime]:
    """Open a runtime and guarantee that its SQLite connection is closed."""

    async with WorkflowRuntime(
        ingestion_services,
        question_answer_services,
        checkpoint_path=checkpoint_path,
        settings=settings,
    ) as runtime:
        yield runtime
