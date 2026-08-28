from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from app.db.models import Citation, ModelRun, WorkflowRequest
from app.providers.rag import AnswerClaim, AnswerDraft
from app.workflows.question_answer_production import (
    ProductionQuestionAnswerWorkflowServices,
    QuestionAnswerWorkflowCoordinator,
)
from app.workflows.runtime import WorkflowRuntime
from sqlalchemy import select


class CountingProvider:
    provider = "stage6-fake-rag"
    model = "stage6-fake-rag-v1"
    prompt_version = "stage6-fake-rag-v1"

    def __init__(self, claim: str = "确定性回答。") -> None:
        self.answer_calls = 0
        self.claim = claim

    async def answer(
        self, _query: str, _evidence: Sequence[Mapping[str, str]]
    ) -> AnswerDraft:
        self.answer_calls += 1
        return AnswerDraft(claims=(AnswerClaim(self.claim, ("C1",)),))

    async def rewrite_query(self, query: str) -> str:
        return query


def _publish_text(client, content: str) -> None:
    submitted = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": "markdown"},
    )
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    item_id = submitted.json()["item_id"]
    from conftest import wait_for_job

    wait_for_job(client, job_id)
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    assert client.post(f"/api/items/{item_id}/publish").status_code == 200


async def _audit_rows(session_factory):
    async with session_factory() as session:
        return (
            list((await session.execute(select(ModelRun))).scalars()),
            list((await session.execute(select(Citation))).scalars()),
            list((await session.execute(select(WorkflowRequest))).scalars()),
        )


def test_question_answer_graph_uses_atomic_service_and_is_idempotent(client) -> None:
    _publish_text(client, "SQLite 是当前回答的确定性证据。")
    provider = CountingProvider()
    client.app.state.question_answer_service.chat_provider = provider
    request_id = str(uuid4())

    first = client.post(
        "/api/chat/stream",
        json={"request_id": request_id, "query": "SQLite 确定性证据"},
    )
    duplicate = client.post(
        "/api/chat/stream",
        json={"request_id": request_id, "query": "完全不同的问题"},
    )

    assert first.status_code == 200, first.text
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "idempotency_conflict"
    assert first.text != duplicate.text
    assert provider.answer_calls == 1
    runs, citations, requests = client.portal.call(
        _audit_rows, client.app.state.session_factory
    )
    assert len([run for run in runs if run.status == "succeeded"]) == 1
    assert len(citations) == 1
    assert len(requests) == 1
    assert requests[0].id == request_id
    assert requests[0].status == "succeeded"

    state = client.portal.call(
        client.app.state.workflow_runtime.snapshot_question_answer, request_id
    )
    assert state["route"] == "completed"
    assert "answer" not in state.state
    assert "evidence" not in state.state
    assert "C1" in state["citation_ids"]


def test_refusal_route_never_calls_answer_provider_and_is_durable(client) -> None:
    provider = CountingProvider()
    client.app.state.question_answer_service.chat_provider = provider
    request_id = str(uuid4())

    first = client.post(
        "/api/chat/stream",
        json={"request_id": request_id, "query": "不存在的合成知识"},
    )
    duplicate = client.post(
        "/api/chat/stream",
        json={"request_id": request_id, "query": "另一条查询"},
    )

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "idempotency_conflict"
    assert "证据不足" in first.text
    assert first.text != duplicate.text
    assert provider.answer_calls == 0
    runs, citations, requests = client.portal.call(
        _audit_rows, client.app.state.session_factory
    )
    assert runs == []
    assert citations == []
    assert requests[0].status == "refused"
    assert json.loads(requests[0].result_json)["refusal_code"] == "insufficient_evidence"


def test_request_identity_binds_query_and_all_result_options(client) -> None:
    _publish_text(client, "SQLite 查询身份选项的确定性证据。")
    provider = CountingProvider()
    client.app.state.question_answer_service.chat_provider = provider
    query = "SQLite 查询身份选项"
    option_changes = (
        ({"limit": 6}, {"limit": 7}),
        ({"rewrite": "off"}, {"rewrite": "auto"}),
        ({"source_types": ["markdown"]}, {"source_types": ["text"]}),
    )

    for first_options, second_options in option_changes:
        request_id = str(uuid4())
        first = client.post(
            "/api/chat/stream",
            json={"request_id": request_id, "query": query, **first_options},
        )
        conflict = client.post(
            "/api/chat/stream",
            json={"request_id": request_id, "query": query, **second_options},
        )
        assert first.status_code == 200, first.text
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

    runs, citations, requests = client.portal.call(
        _audit_rows, client.app.state.session_factory
    )
    assert len([run for run in runs if run.status == "succeeded"]) == 3
    assert len(citations) == 3
    assert len(requests) == 3
    for request in requests:
        persisted = json.loads(request.parameters_json)
        assert query not in request.parameters_json
        assert len(persisted["fingerprint"]["query_sha256"]) == 64
        assert persisted["fingerprint"]["mode"] == "answer"


def test_same_request_id_concurrent_calls_share_one_provider_and_result(client) -> None:
    _publish_text(client, "并发请求必须只生成一次答案和引用。")
    provider = CountingProvider()
    client.app.state.question_answer_service.chat_provider = provider
    request_id = str(uuid4())

    async def run_concurrently():
        payload = {
            "request_id": request_id,
            "query": "并发请求必须只生成一次答案和引用",
            "mode": "answer",
        }
        return await asyncio.gather(
            client.app.state.question_answer_workflow.run(payload),
            client.app.state.question_answer_workflow.run(payload),
        )

    first, second = client.portal.call(run_concurrently)
    assert first.model_run_id == second.model_run_id
    assert first.citations[0].citation_id == second.citations[0].citation_id
    assert provider.answer_calls == 1
    runs, citations, requests = client.portal.call(
        _audit_rows, client.app.state.session_factory
    )
    assert len([run for run in runs if run.status == "succeeded"]) == 1
    assert len(citations) == 1
    assert len(requests) == 1


def test_result_survives_new_graph_runtime_when_checkpoint_was_not_advanced(
    client, tmp_path: Path
) -> None:
    _publish_text(client, "SQLite checkpoint 崩溃恢复需要稳定请求边界。")
    provider = CountingProvider(
        "答案包含 api_key=API_KEY_SENTINEL，但公开边界必须脱敏。"
    )
    client.app.state.question_answer_service.chat_provider = provider
    request_id = str(uuid4())
    query = "SQLite 崩溃恢复 api_key=API_KEY_SENTINEL"

    first = client.post(
        "/api/chat/stream",
        json={"request_id": request_id, "query": query},
    )
    assert first.status_code == 200, first.text
    assert "API_KEY_SENTINEL" not in first.text

    async def rerun_with_new_checkpoint():
        services = ProductionQuestionAnswerWorkflowServices(
            client.app.state.question_answer_service
        )
        async with WorkflowRuntime(
            question_answer_services=services,
            checkpoint_path=tmp_path / "replacement-checkpoint.db",
        ) as runtime:
            coordinator = QuestionAnswerWorkflowCoordinator(runtime, services)
            return await coordinator.run(
                {
                    "request_id": request_id,
                    "query": query,
                    "mode": "answer",
                }
            )

    restored = client.portal.call(rerun_with_new_checkpoint)
    assert restored.model_run_id
    assert restored.answer == "答案包含 [REDACTED]"
    assert provider.answer_calls == 1
    raw_checkpoint = (tmp_path / "replacement-checkpoint.db").read_bytes()
    assert b"API_KEY_SENTINEL" not in raw_checkpoint
    assert b"Authorization" not in raw_checkpoint
