from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.rag.evaluation import evaluate_rankings, load_eval_cases
from app.rag.reranking import KeywordOverlapReranker
from app.rag.types import RetrievedChunk

from conftest import wait_for_job


class FailingReranker:
    name = "failing-test-reranker"
    model = "failure-v1"

    async def rerank(self, _query, _chunks):
        raise RuntimeError("synthetic reranker failure")


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


def _chunk(chunk_id: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        knowledge_item_id="item",
        content_version_id="version",
        item_title="测试",
        version_no=1,
        source_type="markdown",
        content=content,
        source_locator="Notes/test.md",
    )


@pytest.mark.asyncio
async def test_optional_keyword_reranker_changes_order_and_records_score(
    client: TestClient,
) -> None:
    retriever = client.app.state.rag_retriever
    retriever.reranker = KeywordOverlapReranker()

    chunks = await retriever._apply_reranker(
        "SQLite 权威校验",
        [
            _chunk("weak", "只有 SQLite"),
            _chunk("strong", "SQLite 权威校验 是当前规则"),
        ],
    )

    assert [chunk.chunk_id for chunk in chunks] == ["strong", "weak"]
    assert chunks[0].rerank_score == pytest.approx(1.0)
    assert chunks[1].rerank_score == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_reranker_failure_keeps_rrf_results_and_marks_degraded(
    client: TestClient,
) -> None:
    _publish_text(client, "SQLite RRF 结果仍然应该可以返回。")
    retriever = client.app.state.rag_retriever
    retriever.reranker = FailingReranker()

    chunks, diagnostics, assessment = await retriever.retrieve("SQLite RRF", limit=3)

    assert chunks
    assert assessment.status == "sufficient"
    assert diagnostics.degraded is True
    assert diagnostics.reranker_available is False
    assert diagnostics.channel_errors["reranker"] == "RuntimeError"
    assert all(chunk.rerank_score is None for chunk in chunks)


def test_fixed_chinese_eval_has_explicit_thresholds() -> None:
    cases = load_eval_cases(Path(__file__).parent / "fixtures" / "rag_eval_cases.json")
    rankings = {
        "sqlite-authority": ["eval-sqlite", "noise"],
        "citation-locator": ["noise", "eval-citation"],
        "obsidian-source": ["eval-obsidian", "noise"],
        "evidence-refusal": ["noise", "eval-evidence"],
    }

    metrics = evaluate_rankings(cases, rankings, k=2)

    assert metrics["case_count"] == 4
    assert metrics["recall_at_k"] >= 1.0
    assert metrics["hit_rate_at_k"] >= 1.0
    assert metrics["mrr_at_k"] >= 0.75
