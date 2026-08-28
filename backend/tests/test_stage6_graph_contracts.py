from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.workflows import (
    IngestionInput,
    IngestionProcessResult,
    IngestionPublishResult,
    IngestionResumeDecision,
    IngestionStateModel,
    QuestionAnswerAnswerResult,
    QuestionAnswerInput,
    QuestionAnswerRetrievalResult,
    QuestionAnswerStateModel,
    WorkflowRuntime,
)


def identifier(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012x}"


JOB_ID = identifier(1)
ITEM_ID = identifier(2)
CONTENT_VERSION_ID = identifier(3)
REQUEST_ID = identifier(4)
MODEL_RUN_ID = identifier(5)
CITATION_ID = identifier(6)


class DeterministicIngestionServices:
    async def process(self, **_: object) -> IngestionProcessResult:
        return IngestionProcessResult(content_version_id=CONTENT_VERSION_ID)

    async def review(self, **_: object) -> None:
        return None

    async def abandon(self, **_: object) -> None:
        return None

    async def publish(self, **_: object) -> IngestionPublishResult:
        return IngestionPublishResult(content_version_id=CONTENT_VERSION_ID)


class DeterministicQuestionAnswerServices:
    def __init__(self, evidence_status: str) -> None:
        self.evidence_status = evidence_status
        self.answer_calls = 0

    async def retrieve(self, **_: object) -> QuestionAnswerRetrievalResult:
        citations = [CITATION_ID] if self.evidence_status == "sufficient" else []
        return QuestionAnswerRetrievalResult(
            evidence_status=self.evidence_status,
            citation_ids=citations,
        )

    async def answer(self, **_: object) -> QuestionAnswerAnswerResult:
        self.answer_calls += 1
        return QuestionAnswerAnswerResult(
            model_run_id=MODEL_RUN_ID,
            citation_ids=[CITATION_ID],
        )


def test_boundary_models_are_strict_and_fail_closed() -> None:
    valid = {
        "job_id": JOB_ID,
        "item_id": ITEM_ID,
        "source_type": "text",
    }
    with pytest.raises(ValidationError):
        IngestionInput.model_validate({**valid, "unexpected": "value"})
    with pytest.raises(ValidationError):
        IngestionInput.model_validate({**valid, "job_id": "not-an-id"})
    with pytest.raises(ValidationError):
        IngestionResumeDecision.model_validate({"decision": "maybe"})

    with pytest.raises(ValidationError):
        IngestionStateModel(
            job_id=JOB_ID,
            item_id=ITEM_ID,
            source_type="text",
            stage="not-a-stage",
        )
    with pytest.raises(ValidationError):
        QuestionAnswerStateModel(
            request_id=REQUEST_ID,
            safe_query="hello",
            mode="answer",
            evidence_status="unknown",
            route="not-a-route",
        )
    with pytest.raises(ValidationError):
        QuestionAnswerInput.model_validate(
            {"request_id": REQUEST_ID, "query": "hello", "extra": True}
        )


def test_query_boundary_redacts_secrets_and_rejects_traces() -> None:
    query = (
        "api_key=API_KEY_SENTINEL Authorization: Bearer AUTH_SENTINEL "
        "Cookie: COOKIE_SENTINEL C:\\Users\\Lenovo\\Vault Root\\note.md"
    )
    safe_input = QuestionAnswerInput(request_id=REQUEST_ID, query=query)
    assert "API_KEY_SENTINEL" not in safe_input.safe_query
    assert "AUTH_SENTINEL" not in safe_input.safe_query
    assert "COOKIE_SENTINEL" not in safe_input.safe_query
    assert "C:\\Users\\Lenovo\\Vault" not in safe_input.safe_query

    with pytest.raises(ValidationError):
        QuestionAnswerInput(
            request_id=REQUEST_ID,
            query="Traceback (most recent call last): TRACEBACK_SENTINEL",
        )


@pytest.mark.asyncio
async def test_both_graphs_compile_and_run_deterministically(tmp_path: Path) -> None:
    ingestion = DeterministicIngestionServices()
    question_answer = DeterministicQuestionAnswerServices("sufficient")
    async with WorkflowRuntime(
        ingestion,
        question_answer,
        checkpoint_path=tmp_path / "contracts.db",
    ) as runtime:
        assert {
            "__start__",
            "validate",
            "route_source",
            "process",
            "review_gate",
            "review_decision",
            "publish_gate",
            "publish_decision",
            "publish",
            "__end__",
        }.issubset(runtime.ingestion_graph.get_graph().nodes)
        assert {
            "__start__",
            "validate",
            "classify",
            "retrieve",
            "evidence_gate",
            "refuse",
            "answer",
            "__end__",
        }.issubset(runtime.question_answer_graph.get_graph().nodes)

        ingestion_run = await runtime.run_ingestion(
            {"job_id": JOB_ID, "item_id": ITEM_ID, "source_type": "text"},
            thread_id=JOB_ID,
        )
        assert ingestion_run["stage"] == "review_gate"
        assert ingestion_run.interrupted
        assert "thread_id" not in ingestion_run.state

        answer_run = await runtime.run_question_answer(
            {"request_id": REQUEST_ID, "query": "  what is RAG?  "},
            thread_id=REQUEST_ID,
        )
        assert answer_run["safe_query"] == "what is RAG?"
        assert answer_run["normalized_query"] == "what is RAG?"
        assert answer_run["route"] == "completed"
        assert answer_run["model_run_id"] == MODEL_RUN_ID
        assert question_answer.answer_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_status", ["none", "low_confidence", "unknown"])
async def test_insufficient_evidence_refuses_without_answer_call(
    tmp_path: Path, evidence_status: str
) -> None:
    services = DeterministicQuestionAnswerServices(evidence_status)
    async with WorkflowRuntime(
        question_answer_services=services,
        checkpoint_path=tmp_path / "refuse.db",
    ) as runtime:
        result = await runtime.run_question_answer(
            {"request_id": REQUEST_ID, "query": "unsupported question"},
            thread_id=REQUEST_ID,
        )

    assert result["route"] == "completed"
    assert result["refusal_code"] == "insufficient_evidence"
    assert result["model_run_id"] is None
    assert services.answer_calls == 0
