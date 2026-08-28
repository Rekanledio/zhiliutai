import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from app.rag.query import QueryProcessor
from app.rag.retrieval import (
    ChannelHit,
    EvidencePolicy,
    HybridRetriever,
    reciprocal_rank_fusion,
)
from app.rag.types import RetrievedChunk
from app.services.vector_store import COLLECTION_NAME, QdrantLocalStore, VectorRecord
from conftest import wait_for_job


def _publish(client: TestClient, content: str, source_type: str = "markdown") -> str:
    submitted = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": source_type},
    )
    assert submitted.status_code == 202, submitted.text
    item_id = submitted.json()["item_id"]
    wait_for_job(client, submitted.json()["job_id"])
    assert client.post(f"/api/items/{item_id}/review", json={}).status_code == 200
    published = client.post(f"/api/items/{item_id}/publish")
    assert published.status_code == 200, published.text
    return item_id


def _submit_pending(client: TestClient, content: str) -> str:
    submitted = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": "markdown"},
    )
    assert submitted.status_code == 202, submitted.text
    wait_for_job(client, submitted.json()["job_id"])
    return submitted.json()["item_id"]


def test_query_processor_normalizes_and_quotes_fts_tokens() -> None:
    processed = QueryProcessor().process("  ＲＡＧ\t检索 OR (安全) *  ")

    assert processed.normalized == "RAG 检索 OR (安全) *"
    assert processed.tokens == ("RAG", "检索", "OR", "安全")
    assert processed.fts_query == '"RAG" OR "检索" OR "OR" OR "安全"'


def test_rrf_deduplicates_and_orders_ties_deterministically() -> None:
    fused = reciprocal_rank_fusion(
        {
            "fts": [ChannelHit("b", 1, -1.0), ChannelHit("a", 2, -2.0)],
            "vector": [ChannelHit("a", 1, 0.9), ChannelHit("c", 2, 0.7)],
        },
        k=60,
    )

    assert [hit.chunk_id for hit in fused] == ["a", "b", "c"]
    assert fused[0].matched_by == ("fts", "vector")
    assert fused[0].fts_rank == 2
    assert fused[0].vector_rank == 1
    assert fused[0].rrf_score == pytest.approx(1 / 62 + 1 / 61)


def test_rrf_prefers_stable_chunk_key_over_random_chunk_id() -> None:
    fused = reciprocal_rank_fusion(
        {
            "fts": [
                ChannelHit(
                    "random-z",
                    1,
                    stable_key=("item", "version", 0, "chunk-a", "markdown", "inbox", "A"),
                ),
                ChannelHit(
                    "random-a",
                    1,
                    stable_key=("item", "version", 1, "chunk-b", "markdown", "inbox", "B"),
                ),
            ]
        }
    )

    assert [hit.chunk_id for hit in fused] == ["random-z", "random-a"]


def test_evidence_policy_distinguishes_none_low_and_sufficient() -> None:
    base = dict(
        chunk_id="chunk",
        knowledge_item_id="item",
        content_version_id="version",
        item_title="标题",
        version_no=1,
        source_type="markdown",
        content="证据",
        source_locator="Notes/test.md",
    )
    policy = EvidencePolicy(vector_score_threshold=0.8, fts_confident_rank=2)

    assert policy.assess([]).status == "none"
    assert policy.assess([RetrievedChunk(**base, vector_rank=1, vector_score=0.2)]).status == (
        "low_confidence"
    )
    assert policy.assess([RetrievedChunk(**base, fts_rank=1)]).status == "sufficient"
    assert policy.assess([RetrievedChunk(**base, vector_rank=1, vector_score=0.9)]).status == (
        "sufficient"
    )


@pytest.mark.asyncio
async def test_hybrid_retrieval_uses_sqlite_current_published_authority(
    client: TestClient, settings
) -> None:
    current_item_id = _publish(client, "当前版本包含 SQLite FTS5 权威证据。")
    _submit_pending(client, "待审核版本也包含 SQLite FTS5，但不能被搜索。")

    with sqlite3.connect(settings.database_path) as connection:
        current_version_id = connection.execute(
            "SELECT current_content_version_id FROM knowledge_items WHERE id = ?",
            (current_item_id,),
        ).fetchone()[0]
        old_version_id = "old-version-for-rag"
        old_chunk_id = "old-chunk-for-rag"
        connection.execute(
            "INSERT INTO content_versions "
            "(id, knowledge_item_id, version_no, source_kind, title, body, content_hash, "
            "summary, suggested_tags_json, prompt_version, source_metadata_json, created_at) "
            "SELECT ?, knowledge_item_id, 99, source_kind, title, "
            "'旧版本 SQLite FTS5 污染证据', content_hash, summary, suggested_tags_json, "
            "prompt_version, source_metadata_json, created_at "
            "FROM content_versions WHERE id = ?",
            (old_version_id, current_version_id),
        )
        connection.execute(
            "INSERT INTO chunks "
            "(id, knowledge_item_id, content_version_id, ordinal, content, content_hash, "
            "source_type, source_locator, embedding_model, embedding_version, qdrant_point_id) "
            "SELECT ?, knowledge_item_id, ?, 0, '旧版本 SQLite FTS5 污染证据', content_hash, "
            "source_type, source_locator, embedding_model, embedding_version, NULL "
            "FROM chunks WHERE content_version_id = ? LIMIT 1",
            (old_chunk_id, old_version_id, current_version_id),
        )
        connection.execute(
            "INSERT INTO chunk_fts "
            "(chunk_id, knowledge_item_id, content_version_id, content, source_locator) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                old_chunk_id,
                current_item_id,
                old_version_id,
                "旧版本 SQLite FTS5 污染证据",
                "old",
            ),
        )
        connection.commit()

    retriever = HybridRetriever(
        client.app.state.session_factory,
        client.app.state.stage2_service.vector_store,
        client.app.state.stage2_service.embedding_provider,
        settings,
    )
    chunks, diagnostics, assessment = await retriever.retrieve("SQLite FTS5", limit=10)

    assert diagnostics.fts_available is True
    assert assessment.status == "sufficient"
    assert chunks
    assert all(chunk.content_version_id == current_version_id for chunk in chunks)
    assert old_chunk_id not in {chunk.chunk_id for chunk in chunks}

    client.delete(f"/api/items/{current_item_id}")
    deleted_chunks, _, deleted_assessment = await retriever.retrieve("SQLite FTS5", limit=10)
    assert current_item_id not in {chunk.knowledge_item_id for chunk in deleted_chunks}
    assert deleted_assessment.status in {"none", "low_confidence"}


@pytest.mark.asyncio
async def test_hybrid_retrieval_drops_qdrant_payload_mismatch_with_sqlite(
    client: TestClient,
) -> None:
    item_id = _publish(client, "Qdrant payload 必须与 SQLite 当前版本完全一致。")
    database_path = client.app.state.settings.database_path
    with sqlite3.connect(database_path) as connection:
        point_id = connection.execute(
            "SELECT qdrant_point_id FROM chunks WHERE knowledge_item_id = ?",
            (item_id,),
        ).fetchone()[0]
    qdrant = QdrantClient(path=str(client.app.state.settings.qdrant_path))
    try:
        qdrant.set_payload(
            collection_name=COLLECTION_NAME,
            payload={"source_locator": "Notes/forged-locator.md"},
            points=[point_id],
        )
    finally:
        qdrant.close()

    chunks, _diagnostics, _assessment = await client.app.state.rag_retriever.retrieve(
        "Qdrant payload SQLite 当前版本", limit=5
    )
    matching = [chunk for chunk in chunks if chunk.knowledge_item_id == item_id]
    assert matching
    assert all("vector" not in chunk.matched_by for chunk in matching)


def test_qdrant_search_filters_current_version(tmp_path: Path) -> None:
    store = QdrantLocalStore(tmp_path / "qdrant", 3)
    current = VectorRecord(
        point_id="00000000-0000-0000-0000-000000000001",
        vector=[1.0, 0.0, 0.0],
        chunk_id="current-chunk",
        knowledge_item_id="item",
        content_version_id="current-version",
        source_type="markdown",
        source_locator="Notes/current.md",
        embedding_model="fake",
        embedding_version="v1",
    )
    stale = VectorRecord(
        point_id="00000000-0000-0000-0000-000000000002",
        vector=[0.99, 0.01, 0.0],
        chunk_id="stale-chunk",
        knowledge_item_id="item",
        content_version_id="stale-version",
        source_type="markdown",
        source_locator="Notes/stale.md",
        embedding_model="fake",
        embedding_version="v1",
    )
    store.upsert([current, stale])

    results = store.search(
        [1.0, 0.0, 0.0],
        limit=10,
        content_version_ids=["current-version"],
    )

    assert [result["payload"]["chunk_id"] for result in results] == ["current-chunk"]
