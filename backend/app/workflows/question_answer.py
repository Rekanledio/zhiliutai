"""Deterministic, provider-neutral question-answer workflow skeleton."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from pydantic import ValidationError

from app.workflows.contracts import (
    QuestionAnswerState,
    QuestionAnswerStateModel,
    QuestionAnswerWorkflowServices,
    QuestionAnswerAnswerResult,
    QuestionAnswerInput,
    QuestionAnswerRetrievalResult,
)


def _parse_state(state: Mapping[str, object]) -> QuestionAnswerStateModel:
    return QuestionAnswerStateModel.model_validate(dict(state))


def _failure(error_code: Any) -> QuestionAnswerState:
    return {"route": "failed", "error_code": error_code}


async def _validate(state: QuestionAnswerState) -> QuestionAnswerState:
    try:
        current = _parse_state(state)
    except ValidationError:
        return _failure("question_answer_invalid_state")
    if current.route != "validate":
        return _failure("question_answer_invalid_state")
    return {
        "normalized_query": current.safe_query,
        "route": "classify",
        "error_code": None,
    }


def _classify(state: QuestionAnswerState) -> QuestionAnswerState:
    try:
        current = _parse_state(state)
    except ValidationError:
        return _failure("question_answer_invalid_state")
    if current.route != "classify" or current.normalized_query is None:
        return _failure("question_answer_invalid_state")
    return {"route": "retrieve", "error_code": None}


def _service_retrieve_node(services: QuestionAnswerWorkflowServices):
    async def retrieve(state: QuestionAnswerState) -> QuestionAnswerState:
        try:
            current = _parse_state(state)
        except ValidationError:
            return _failure("question_answer_invalid_state")
        if current.route != "retrieve" or current.normalized_query is None:
            return _failure("question_answer_invalid_state")
        try:
            result = await services.retrieve(
                request_id=current.request_id,
                safe_query=current.safe_query,
                normalized_query=current.normalized_query,
                mode=current.mode,
            )
        except Exception:
            return _failure("question_answer_retrieve_failed")
        try:
            safe_result = QuestionAnswerRetrievalResult.model_validate(result)
        except ValidationError:
            return _failure("question_answer_invalid_result")
        return {
            "evidence_status": safe_result.evidence_status,
            "citation_ids": safe_result.citation_ids,
            "route": "evidence_gate",
            "error_code": None,
        }

    return retrieve


def _after_retrieve(state: QuestionAnswerState) -> str:
    return "evidence_gate" if state.get("route") == "evidence_gate" else "failed"


def _evidence_gate(state: QuestionAnswerState) -> QuestionAnswerState:
    try:
        current = _parse_state(state)
    except ValidationError:
        return _failure("question_answer_invalid_state")
    if current.route != "evidence_gate":
        return _failure("question_answer_invalid_state")
    if current.evidence_status == "sufficient":
        return {"route": "answer", "error_code": None}
    return {"route": "refuse", "error_code": None}


def _after_evidence_gate(state: QuestionAnswerState) -> str:
    route = state.get("route")
    return route if route in {"answer", "refuse"} else "failed"


def _refuse(state: QuestionAnswerState) -> QuestionAnswerState:
    try:
        current = _parse_state(state)
    except ValidationError:
        return _failure("question_answer_invalid_state")
    if current.route != "refuse":
        return _failure("question_answer_invalid_state")
    return {
        "route": "completed",
        "refusal_code": "insufficient_evidence",
        "error_code": None,
    }


def _service_answer_node(services: QuestionAnswerWorkflowServices):
    async def answer(state: QuestionAnswerState) -> QuestionAnswerState:
        try:
            current = _parse_state(state)
        except ValidationError:
            return _failure("question_answer_invalid_state")
        if current.route != "answer" or current.normalized_query is None:
            return _failure("question_answer_invalid_state")
        if current.evidence_status != "sufficient":
            return _failure("question_answer_invalid_state")
        try:
            result = await services.answer(
                request_id=current.request_id,
                safe_query=current.safe_query,
                normalized_query=current.normalized_query,
                citation_ids=list(current.citation_ids),
            )
        except Exception:
            return _failure("question_answer_answer_failed")
        try:
            safe_result = QuestionAnswerAnswerResult.model_validate(result)
        except ValidationError:
            return _failure("question_answer_invalid_result")
        return {
            "route": "completed",
            "model_run_id": safe_result.model_run_id,
            "citation_ids": safe_result.citation_ids,
            "refusal_code": None,
            "error_code": None,
        }

    return answer


class _SafeQuestionAnswerGraph:
    """Guard the lower-level compiled graph from unvalidated state input."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if isinstance(input, Command):
            raise ValueError("question-answer graph does not accept Commands")
        elif isinstance(input, QuestionAnswerInput):
            input = input.to_state()
        if input is not None:
            if not isinstance(input, Mapping):
                raise ValueError("question-answer graph input must be a safe state")
            if "route" not in input:
                input = QuestionAnswerInput.model_validate(dict(input)).to_state()
            else:
                input = QuestionAnswerStateModel.model_validate(dict(input)).to_state()
        return await self._graph.ainvoke(input, config=config, **kwargs)


def build_question_answer_graph(
    services: QuestionAnswerWorkflowServices,
    *,
    checkpointer: Any = None,
):
    """Compile the QA skeleton with retrieval and answer services injected."""

    builder = StateGraph(QuestionAnswerState)
    builder.add_node("validate", _validate)
    builder.add_node("classify", _classify)
    builder.add_node("retrieve", _service_retrieve_node(services))
    builder.add_node("evidence_gate", _evidence_gate)
    builder.add_node("refuse", _refuse)
    builder.add_node("answer", _service_answer_node(services))

    builder.add_edge(START, "validate")
    builder.add_edge("validate", "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_conditional_edges(
        "retrieve",
        _after_retrieve,
        {"evidence_gate": "evidence_gate", "failed": END},
    )
    builder.add_conditional_edges(
        "evidence_gate",
        _after_evidence_gate,
        {"answer": "answer", "refuse": "refuse", "failed": END},
    )
    builder.add_edge("answer", END)
    builder.add_edge("refuse", END)
    return _SafeQuestionAnswerGraph(
        builder.compile(checkpointer=checkpointer, name="QuestionAnswerGraph")
    )
