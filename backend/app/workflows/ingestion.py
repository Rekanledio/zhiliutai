"""Deterministic, service-only ingestion workflow skeleton."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from app.workflows.contracts import (
    IngestionInput,
    IngestionInterruptPayload,
    IngestionProcessResult,
    IngestionPublishResult,
    IngestionResumeDecision,
    IngestionState,
    IngestionStateModel,
    IngestionWorkflowServices,
)


def _parse_state(state: Mapping[str, object]) -> IngestionStateModel:
    return IngestionStateModel.model_validate(dict(state))


def _failure(error_code: Any) -> IngestionState:
    return {"stage": "failed", "error_code": error_code}


async def _validate(state: IngestionState) -> IngestionState:
    try:
        current = _parse_state(state)
    except ValidationError:
        return _failure("ingestion_invalid_state")
    if current.stage != "validate":
        return _failure("ingestion_invalid_state")
    return {"stage": "route_source", "error_code": None}


async def _route_source(state: IngestionState) -> IngestionState:
    try:
        current = _parse_state(state)
    except ValidationError:
        return _failure("ingestion_invalid_state")
    if current.stage != "route_source":
        return _failure("ingestion_invalid_state")
    return {"stage": "process", "error_code": None}


def _service_process_node(services: IngestionWorkflowServices):
    async def process(state: IngestionState) -> IngestionState:
        try:
            current = _parse_state(state)
        except ValidationError:
            return _failure("ingestion_invalid_state")
        if current.stage != "process":
            return _failure("ingestion_invalid_state")
        try:
            result = await services.process(
                job_id=current.job_id,
                item_id=current.item_id,
                source_type=current.source_type,
            )
        except Exception:
            return _failure("ingestion_process_failed")
        try:
            safe_result = IngestionProcessResult.model_validate(result)
        except ValidationError:
            return _failure("ingestion_invalid_result")
        if safe_result.status == "asr_required":
            return {
                "stage": "completed",
                "processing_status": "asr_required",
                "result_content_version_id": None,
                "error_code": None,
            }
        if safe_result.content_version_id is None:
            return _failure("ingestion_invalid_result")
        return {
            "stage": "review_gate",
            "result_content_version_id": safe_result.content_version_id,
            "processing_status": "ready",
            "error_code": None,
        }

    return process


def _review_gate_payload(current: IngestionStateModel) -> dict[str, object]:
    payload = IngestionInterruptPayload(
        kind="review_required",
        stage="review_gate",
        job_id=current.job_id,
        item_id=current.item_id,
        source_type=current.source_type,
        result_content_version_id=current.result_content_version_id,
    )
    return payload.model_dump(mode="json")


async def _review_gate(state: IngestionState) -> IngestionState:
    try:
        current = _parse_state(state)
    except ValidationError:
        return _failure("ingestion_invalid_state")
    if current.stage != "review_gate" or current.result_content_version_id is None:
        return _failure("ingestion_invalid_state")

    decision_value = interrupt(_review_gate_payload(current))
    try:
        decision = IngestionResumeDecision.model_validate(decision_value)
    except ValidationError:
        return _failure("ingestion_invalid_resume")
    return {
        "stage": "review_decision",
        "review_decision": decision.decision,
        "error_code": None,
    }


def _service_review_node(services: IngestionWorkflowServices):
    async def review_decision(state: IngestionState) -> IngestionState:
        try:
            current = _parse_state(state)
        except ValidationError:
            return _failure("ingestion_invalid_state")
        if current.stage != "review_decision" or current.review_decision is None:
            return _failure("ingestion_invalid_state")
        try:
            await services.review(
                job_id=current.job_id,
                item_id=current.item_id,
                decision=current.review_decision,
            )
        except Exception:
            return _failure("ingestion_review_failed")
        if current.review_decision == "approve":
            return {"stage": "publish_gate", "error_code": None}
        if current.review_decision == "reject":
            return {"stage": "rejected", "error_code": None}
        return {"stage": "cancelled", "error_code": None}

    return review_decision


def _publish_gate_payload(current: IngestionStateModel) -> dict[str, object]:
    payload = IngestionInterruptPayload(
        kind="publish_required",
        stage="publish_gate",
        job_id=current.job_id,
        item_id=current.item_id,
        source_type=current.source_type,
        result_content_version_id=current.result_content_version_id,
    )
    return payload.model_dump(mode="json")


async def _publish_gate(state: IngestionState) -> IngestionState:
    try:
        current = _parse_state(state)
    except ValidationError:
        return _failure("ingestion_invalid_state")
    if current.stage != "publish_gate" or current.result_content_version_id is None:
        return _failure("ingestion_invalid_state")

    decision_value = interrupt(_publish_gate_payload(current))
    try:
        decision = IngestionResumeDecision.model_validate(decision_value)
    except ValidationError:
        return _failure("ingestion_invalid_resume")
    return {
        "stage": "publish_decision",
        "publish_decision": decision.decision,
        "error_code": None,
    }


def _service_publish_decision_node(services: IngestionWorkflowServices):
    async def publish_decision(state: IngestionState) -> IngestionState:
        try:
            current = _parse_state(state)
        except ValidationError:
            return _failure("ingestion_invalid_state")
        if current.stage != "publish_decision" or current.publish_decision is None:
            return _failure("ingestion_invalid_state")
        if current.publish_decision == "approve":
            return {"stage": "publish", "error_code": None}
        try:
            await services.abandon(
                job_id=current.job_id,
                item_id=current.item_id,
                decision=current.publish_decision,
            )
        except Exception:
            return _failure("ingestion_publish_failed")
        if current.publish_decision == "reject":
            return {"stage": "rejected", "error_code": None}
        return {"stage": "cancelled", "error_code": None}

    return publish_decision


def _service_publish_node(services: IngestionWorkflowServices):
    async def publish(state: IngestionState) -> IngestionState:
        try:
            current = _parse_state(state)
        except ValidationError:
            return _failure("ingestion_invalid_state")
        if current.stage != "publish" or current.result_content_version_id is None:
            return _failure("ingestion_invalid_state")
        try:
            result = await services.publish(
                job_id=current.job_id,
                item_id=current.item_id,
                content_version_id=current.result_content_version_id,
            )
        except Exception:
            return _failure("ingestion_publish_failed")
        try:
            safe_result = IngestionPublishResult.model_validate(result)
        except ValidationError:
            return _failure("ingestion_invalid_result")
        return {
            "stage": "completed",
            "result_content_version_id": safe_result.content_version_id,
            "processing_status": "ready",
            "error_code": None,
        }

    return publish


def _after_process(state: IngestionState) -> str:
    stage = state.get("stage")
    if stage == "review_gate":
        return "review_gate"
    if stage == "completed":
        return "completed"
    return "failed"


def _after_review(state: IngestionState) -> str:
    stage = state.get("stage")
    return stage if stage in {"publish_gate", "rejected", "cancelled"} else "failed"


def _after_publish_decision(state: IngestionState) -> str:
    stage = state.get("stage")
    if stage == "publish":
        return "publish"
    if stage in {"rejected", "cancelled"}:
        return stage
    return "failed"


class _SafeIngestionGraph:
    """Guard the lower-level compiled graph from raw external Commands."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if isinstance(input, Command):
            if input.resume is None or input.update is not None or input.goto:
                raise ValueError("only a validated ingestion resume decision is supported")
            decision = IngestionResumeDecision.model_validate(input.resume)
            input = Command(resume=decision.model_dump(mode="json"))
        elif isinstance(input, IngestionInput):
            input = input.to_state()
        elif input is not None:
            if not isinstance(input, Mapping):
                raise ValueError("ingestion graph input must be a safe state")
            if "stage" not in input:
                input = IngestionInput.model_validate(dict(input)).to_state()
            else:
                input = IngestionStateModel.model_validate(dict(input)).to_state()
        return await self._graph.ainvoke(input, config=config, **kwargs)


def build_ingestion_graph(
    services: IngestionWorkflowServices,
    *,
    checkpointer: Any = None,
):
    """Compile the ingestion skeleton with services held outside graph state."""

    builder = StateGraph(IngestionState)
    builder.add_node("validate", _validate)
    builder.add_node("route_source", _route_source)
    builder.add_node("process", _service_process_node(services))
    builder.add_node("review_gate", _review_gate)
    builder.add_node("review_decision", _service_review_node(services))
    builder.add_node("publish_gate", _publish_gate)
    builder.add_node("publish_decision", _service_publish_decision_node(services))
    builder.add_node("publish", _service_publish_node(services))

    builder.add_edge(START, "validate")
    builder.add_edge("validate", "route_source")
    builder.add_edge("route_source", "process")
    builder.add_conditional_edges(
        "process",
        _after_process,
        {"review_gate": "review_gate", "completed": END, "failed": END},
    )
    builder.add_edge("review_gate", "review_decision")
    builder.add_conditional_edges(
        "review_decision",
        _after_review,
        {"publish_gate": "publish_gate", "rejected": END, "cancelled": END, "failed": END},
    )
    builder.add_edge("publish_gate", "publish_decision")
    builder.add_conditional_edges(
        "publish_decision",
        _after_publish_decision,
        {"publish": "publish", "rejected": END, "cancelled": END, "failed": END},
    )
    builder.add_edge("publish", END)
    return _SafeIngestionGraph(builder.compile(checkpointer=checkpointer, name="IngestionGraph"))
