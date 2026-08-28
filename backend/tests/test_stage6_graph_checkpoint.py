from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.types import Command
from pydantic import ValidationError

from app.workflows import (
    IngestionProcessResult,
    IngestionPublishResult,
    QuestionAnswerAnswerResult,
    QuestionAnswerRetrievalResult,
    WorkflowCheckpoint,
    WorkflowRuntime,
    thread_config,
)


def identifier(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012x}"


JOB_ID = identifier(101)
ITEM_ID = identifier(102)
CONTENT_VERSION_ID = identifier(103)
REQUEST_ID = identifier(104)
MODEL_RUN_ID = identifier(105)
CITATION_ID = identifier(106)


class CountingIngestionServices:
    def __init__(
        self,
        *,
        fail_process: int = 0,
        fail_review: int = 0,
        fail_publish: int = 0,
    ) -> None:
        self.fail_process = fail_process
        self.fail_review = fail_review
        self.fail_publish = fail_publish
        self.process_calls = 0
        self.review_calls: list[str] = []
        self.publish_calls = 0

    async def process(self, **_: object) -> IngestionProcessResult:
        self.process_calls += 1
        if self.fail_process:
            self.fail_process -= 1
            raise RuntimeError("TRACEBACK_SENTINEL API_KEY_SENTINEL")
        return IngestionProcessResult(content_version_id=CONTENT_VERSION_ID)

    async def review(self, **kwargs: object) -> None:
        decision = kwargs.get("decision")
        assert isinstance(decision, str)
        self.review_calls.append(decision)
        if self.fail_review:
            self.fail_review -= 1
            raise RuntimeError("review failure with AUTHORIZATION_SENTINEL")

    async def abandon(self, **_: object) -> None:
        return None

    async def publish(self, **_: object) -> IngestionPublishResult:
        self.publish_calls += 1
        if self.fail_publish:
            self.fail_publish -= 1
            raise RuntimeError("publish failure with COOKIE_SENTINEL")
        return IngestionPublishResult(content_version_id=CONTENT_VERSION_ID)


class DeterministicQuestionAnswerServices:
    def __init__(self, evidence_status: str = "sufficient") -> None:
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


class FailingQuestionAnswerServices(DeterministicQuestionAnswerServices):
    async def retrieve(self, **_: object) -> QuestionAnswerRetrievalResult:
        raise RuntimeError("TRACEBACK_SENTINEL API_KEY_SENTINEL")


@pytest.mark.asyncio
async def test_checkpoint_is_created_closed_and_reopened(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    connection = None
    async with WorkflowCheckpoint(path) as checkpoint:
        connection = checkpoint.saver.conn
        async with connection.execute("PRAGMA journal_mode") as cursor:
            journal_mode = (await cursor.fetchone())[0]
        async with connection.execute("PRAGMA busy_timeout") as cursor:
            busy_timeout = (await cursor.fetchone())[0]
        assert journal_mode.casefold() == "wal"
        assert busy_timeout == 5_000
    assert path.exists()
    assert connection is not None
    with pytest.raises(ValueError):
        await connection.execute("SELECT 1")

    async with WorkflowCheckpoint(path) as reopened:
        async with reopened.saver.conn.execute("PRAGMA journal_mode") as cursor:
            assert (await cursor.fetchone())[0].casefold() == "wal"


@pytest.mark.asyncio
async def test_ingestion_review_publish_resume_survives_reopen_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ingestion.db"
    services = CountingIngestionServices()
    async with WorkflowRuntime(services, checkpoint_path=path) as runtime:
        first = await runtime.run_ingestion(
            {"job_id": JOB_ID, "item_id": ITEM_ID, "source_type": "markdown"},
            thread_id=JOB_ID,
        )
        assert first["stage"] == "review_gate"
        assert first.interrupted
        assert (services.process_calls, services.review_calls, services.publish_calls) == (1, [], 0)

        review = await runtime.ingestion_graph.ainvoke(
            Command(resume={"decision": "approve"}),
            config=thread_config(JOB_ID),
        )
        assert review["stage"] == "publish_gate"
        assert review["__interrupt__"]
        assert (services.process_calls, services.review_calls, services.publish_calls) == (1, ["approve"], 0)

    async with WorkflowRuntime(services, checkpoint_path=path) as reopened:
        completed = await reopened.resume_ingestion(JOB_ID, {"decision": "approve"})
        assert completed["stage"] == "completed"
        assert (services.process_calls, services.review_calls, services.publish_calls) == (1, ["approve"], 1)

        duplicate = await reopened.resume_ingestion(JOB_ID, {"decision": "approve"})
        assert duplicate.result == completed.result
        assert (services.process_calls, services.review_calls, services.publish_calls) == (1, ["approve"], 1)


@pytest.mark.asyncio
async def test_ingestion_reject_and_cancel_paths_do_not_publish(tmp_path: Path) -> None:
    services = CountingIngestionServices()
    async with WorkflowRuntime(services, checkpoint_path=tmp_path / "decisions.db") as runtime:
        rejected = await runtime.run_ingestion(
            {"job_id": JOB_ID, "item_id": ITEM_ID, "source_type": "text"},
            thread_id=JOB_ID,
        )
        assert (await runtime.resume_ingestion(JOB_ID, {"decision": "reject"}))["stage"] == "rejected"

        cancelled_id = identifier(107)
        cancelled = await runtime.run_ingestion(
            {"job_id": cancelled_id, "item_id": ITEM_ID, "source_type": "text"},
            thread_id=cancelled_id,
        )
        assert cancelled["stage"] == "review_gate"
        assert (await runtime.resume_ingestion(cancelled_id, {"decision": "cancel"}))["stage"] == "cancelled"

        publish_rejected_id = identifier(108)
        await runtime.run_ingestion(
            {"job_id": publish_rejected_id, "item_id": ITEM_ID, "source_type": "text"},
            thread_id=publish_rejected_id,
        )
        assert (await runtime.resume_ingestion(publish_rejected_id, {"decision": "approve"}))["stage"] == "publish_gate"
        assert (await runtime.resume_ingestion(publish_rejected_id, {"decision": "reject"}))["stage"] == "rejected"
        assert services.publish_calls == 0
        assert rejected["stage"] == "review_gate"


@pytest.mark.asyncio
async def test_failed_node_retries_from_last_successful_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "retry.db"
    services = CountingIngestionServices(fail_process=1)
    async with WorkflowRuntime(services, checkpoint_path=path) as runtime:
        failed = await runtime.run_ingestion(
            {"job_id": JOB_ID, "item_id": ITEM_ID, "source_type": "text"},
            thread_id=JOB_ID,
        )
        assert failed["stage"] == "failed"
        assert failed["error_code"] == "ingestion_process_failed"
        assert services.process_calls == 1

        recovered = await runtime.retry_ingestion(JOB_ID)
        assert recovered["stage"] == "review_gate"
        assert recovered.interrupted
        assert services.process_calls == 2

    review_services = CountingIngestionServices(fail_review=1)
    async with WorkflowRuntime(review_services, checkpoint_path=tmp_path / "review-retry.db") as runtime:
        await runtime.run_ingestion(
            {"job_id": JOB_ID, "item_id": ITEM_ID, "source_type": "text"},
            thread_id=JOB_ID,
        )
        failed = await runtime.resume_ingestion(JOB_ID, {"decision": "approve"})
        assert failed["stage"] == "failed"
        assert failed["error_code"] == "ingestion_review_failed"
        assert review_services.process_calls == 1

        recovered = await runtime.retry_ingestion(JOB_ID)
        assert recovered["stage"] == "publish_gate"
        assert recovered.interrupted
        assert review_services.process_calls == 1
        assert review_services.review_calls == ["approve", "approve"]


@pytest.mark.asyncio
async def test_checkpoint_bytes_contain_no_sensitive_query_or_error_text(tmp_path: Path) -> None:
    path = tmp_path / "security.db"
    malicious_query = (
        "api_key=API_KEY_SENTINEL\n"
        "Authorization: Bearer AUTHORIZATION_SENTINEL\n"
        "Cookie: COOKIE_SENTINEL\n"
        r"C:\Users\Lenovo\Vault Root\note.md"
    )
    services = DeterministicQuestionAnswerServices()
    async with WorkflowRuntime(question_answer_services=services, checkpoint_path=path) as runtime:
        result = await runtime.run_question_answer(
            {"request_id": REQUEST_ID, "query": malicious_query},
            thread_id=REQUEST_ID,
        )
        assert result["route"] == "completed"
        assert "API_KEY_SENTINEL" not in str(result["safe_query"])
        with pytest.raises(ValidationError):
            await runtime.run_question_answer(
                {
                    "request_id": identifier(109),
                    "query": "Traceback (most recent call last): TRACEBACK_SENTINEL",
                },
                thread_id=identifier(109),
            )

    raw_checkpoint = path.read_bytes()
    for sentinel in (
        b"API_KEY_SENTINEL",
        b"AUTHORIZATION_SENTINEL",
        b"COOKIE_SENTINEL",
        b"TRACEBACK_SENTINEL",
        b"C:\\Users\\Lenovo\\Vault Root\\note.md",
    ):
        assert sentinel not in raw_checkpoint

    failing_path = tmp_path / "error-security.db"
    async with WorkflowRuntime(
        question_answer_services=FailingQuestionAnswerServices(),
        checkpoint_path=failing_path,
    ) as runtime:
        failed = await runtime.run_question_answer(
            {"request_id": identifier(110), "query": "safe question"},
            thread_id=identifier(110),
        )
        assert failed["route"] == "failed"
        assert failed["error_code"] == "question_answer_retrieve_failed"
    assert b"TRACEBACK_SENTINEL" not in failing_path.read_bytes()
