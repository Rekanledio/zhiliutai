import json
from collections.abc import Mapping, Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Citation, ModelRun
from app.providers.models import ProviderNotConfigured
from app.providers.rag import AnswerClaim, AnswerDraft
from app.rag.question_answer import AnswerValidationError

from conftest import wait_for_job


class CountingRagProvider:
    provider = "fake-rag"
    model = "fake-rag-v1"
    prompt_version = "fake-rag-test-v1"

    def __init__(self) -> None:
        self.answer_calls = 0
        self.rewrite_calls = 0
        self.last_evidence: list[Mapping[str, str]] = []

    async def answer(
        self, _query: str, evidence: Sequence[Mapping[str, str]]
    ) -> AnswerDraft:
        self.answer_calls += 1
        self.last_evidence = list(evidence)
        return AnswerDraft(
            claims=(
                AnswerClaim(
                    "SQLite 是当前回答使用的权威校验来源。",
                    ("C1",),
                ),
            )
        )

    async def rewrite_query(self, _query: str) -> str:
        self.rewrite_calls += 1
        return _query


class InvalidCitationProvider(CountingRagProvider):
    async def answer(
        self, _query: str, _evidence: Sequence[Mapping[str, str]]
    ) -> AnswerDraft:
        self.answer_calls += 1
        return AnswerDraft(
            claims=(AnswerClaim("没有合法绑定的事实。", ("C999",)),)
        )


def _publish_text(client: TestClient, content: str) -> str:
    submitted = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": "markdown"},
    )
    assert submitted.status_code == 202, submitted.text
    item_id = submitted.json()["item_id"]
    wait_for_job(client, submitted.json()["job_id"])
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    assert client.post(f"/api/items/{item_id}/publish").status_code == 200
    return item_id


def test_sufficient_answer_persists_model_run_and_citation(client: TestClient) -> None:
    _publish_text(client, "SQLite 是 RAG 的权威校验来源。")
    provider = CountingRagProvider()
    service = client.app.state.question_answer_service
    service.chat_provider = provider

    result = client.portal.call(service.answer, "SQLite 权威校验")

    assert result.answer == "SQLite 是当前回答使用的权威校验来源。"
    assert result.model_run_id
    assert provider.answer_calls == 1
    assert provider.last_evidence[0]["citation_id"] == "C1"
    assert "D:\\Work" not in json.dumps(result.as_dict(), ensure_ascii=False)
    runs, citations = client.portal.call(_read_audit_rows, client.app.state.session_factory)
    run = next(run for run in runs if run.id == result.model_run_id)
    assert run.status == "succeeded"
    assert run.input_json
    assert run.output_json
    assert run.input_tokens and run.output_tokens
    assert len([citation for citation in citations if citation.model_run_id == run.id]) == 1


def test_insufficient_evidence_refuses_without_calling_answer_provider(
    client: TestClient,
) -> None:
    provider = CountingRagProvider()
    service = client.app.state.question_answer_service
    service.chat_provider = provider

    result = client.portal.call(service.answer, "不存在的知识")

    assert result.refusal
    assert result.evidence.status == "none"
    assert result.model_run_id is None
    assert provider.answer_calls == 0
    assert provider.rewrite_calls == 0


def test_invalid_claim_citation_fails_run_without_returning_answer(
    client: TestClient,
) -> None:
    _publish_text(client, "SQLite 是可追溯的证据来源。")
    provider = InvalidCitationProvider()
    service = client.app.state.question_answer_service
    service.chat_provider = provider

    with pytest.raises(AnswerValidationError):
        client.portal.call(service.answer, "SQLite 证据")

    runs, citations = client.portal.call(_read_audit_rows, client.app.state.session_factory)
    assert runs[-1].status == "failed"
    assert json.loads(runs[-1].error_json)["code"] == "rag_answer_invalid"
    assert citations == []


def test_sufficient_evidence_requires_configured_chat_provider(client: TestClient) -> None:
    _publish_text(client, "SQLite 是可用的检索证据。")
    service = client.app.state.question_answer_service

    with pytest.raises(ProviderNotConfigured):
        client.portal.call(service.answer, "SQLite 检索证据")


def test_chat_stream_emits_claims_citations_and_done_events(client: TestClient) -> None:
    _publish_text(client, "SQLite 是可追溯的检索证据。")
    provider = CountingRagProvider()
    client.app.state.question_answer_service.chat_provider = provider

    response = client.post(
        "/api/chat/stream",
        json={"query": "SQLite 检索证据"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: meta" in response.text
    assert "event: delta" in response.text
    assert "event: citations" in response.text
    assert "event: done" in response.text
    assert "C1" in response.text
    assert "D:\\Work" not in response.text
    assert "api_key" not in response.text.lower()


def test_chat_stream_returns_refusal_event_without_answer_provider_call(
    client: TestClient,
) -> None:
    provider = CountingRagProvider()
    client.app.state.question_answer_service.chat_provider = provider

    response = client.post(
        "/api/chat/stream",
        json={"query": "完全不存在的知识"},
    )

    assert response.status_code == 200
    assert "证据不足" in response.text
    assert provider.answer_calls == 0


def test_chat_stream_reports_unconfigured_provider_before_streaming(
    client: TestClient,
) -> None:
    _publish_text(client, "SQLite 是可用的检索证据。")

    response = client.post(
        "/api/chat/stream",
        json={"query": "SQLite 检索证据"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "chat_not_configured"


async def _read_audit_rows(session_factory):
    async with session_factory() as session:
        runs = list((await session.execute(select(ModelRun))).scalars().all())
        citations = list((await session.execute(select(Citation))).scalars().all())
        return runs, citations
