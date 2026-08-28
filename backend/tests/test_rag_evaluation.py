import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings, get_settings, sqlite_url_for
from app.db.models import Chunk, ContentVersion, KnowledgeItem
from app.main import create_app
from app.rag.evaluation import EvalCase, evaluate_retriever, load_eval_cases

from conftest import FakeDraftProvider, FakeEmbeddingProvider, migrate, wait_for_job


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


async def _current_chunks(session_factory, item_ids: list[str]) -> list[Chunk]:
    async with session_factory() as session:
        rows = await session.execute(
            select(Chunk)
            .join(ContentVersion, ContentVersion.id == Chunk.content_version_id)
            .join(KnowledgeItem, KnowledgeItem.id == Chunk.knowledge_item_id)
            .where(
                Chunk.knowledge_item_id.in_(item_ids),
                KnowledgeItem.status == "published",
                KnowledgeItem.deleted_at.is_(None),
                KnowledgeItem.current_content_version_id == Chunk.content_version_id,
            )
        )
        return list(rows.scalars())


def _bind_cases(cases: tuple[EvalCase, ...], chunks: list[Chunk]) -> tuple[EvalCase, ...]:
    bound: list[EvalCase] = []
    for case in cases:
        relevant = frozenset(
            chunk.id
            for chunk in chunks
            if any(marker in chunk.content for marker in case.relevant_markers)
        )
        assert relevant, case.case_id
        bound.append(replace(case, relevant_chunk_ids=relevant))
    return tuple(bound)


def _settings_for_eval(root: Path) -> Settings:
    vault = root / "vault"
    vault.mkdir(parents=True)
    return Settings(
        _env_file=None,
        database_url=sqlite_url_for(root / "zhiliutai.db"),
        qdrant_path=root / "qdrant",
        artifact_root=root / "artifacts",
        vault_path=str(vault),
        embedding_dimensions=8,
        obsidian_watch_interval_seconds=0.05,
        health_check_timeout=0.05,
    )


async def _evaluate_app(
    settings: Settings,
    cases: tuple[EvalCase, ...],
) -> dict[str, object]:
    app = create_app(
        settings,
        FakeDraftProvider(),
        FakeEmbeddingProvider(),
        start_background=True,
        serve_frontend=False,
    )
    try:
        with TestClient(app) as eval_client:
            item_ids = [
                _publish_text(
                    eval_client,
                    f"{case.query}\n\n{case.relevant_markers[0]}。这是临时 SQLite 与 Qdrant Local 评测正文。",
                )
                for case in cases
            ]
            chunks = await _current_chunks(eval_client.app.state.session_factory, item_ids)
            bound_cases = _bind_cases(cases, chunks)
            retriever = eval_client.app.state.rag_retriever

            probe_chunks, probe_diagnostics, _probe_assessment = await retriever.retrieve(
                bound_cases[0].query, limit=5
            )
            assert probe_chunks
            assert probe_diagnostics.fts_available is True
            assert probe_diagnostics.vector_available is True
            return await evaluate_retriever(retriever, bound_cases, k=5)
    finally:
        get_settings.cache_clear()


def _evaluate_fresh_environment(
    root: Path,
    cases: tuple[EvalCase, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    root.mkdir(parents=True)
    settings = _settings_for_eval(root)
    migrate(settings, monkeypatch)
    return asyncio.run(_evaluate_app(settings, cases))


def test_fixed_chinese_eval_runs_real_hybrid_retriever_without_quality_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = load_eval_cases(Path(__file__).parent / "fixtures" / "rag_eval_cases.json")
    metrics_runs: list[dict[str, object]] = []
    for run_index in range(4):
        metrics_runs.append(
            _evaluate_fresh_environment(
                tmp_path / f"fresh-eval-{run_index}", cases, monkeypatch
            )
        )
    metrics = metrics_runs[0]
    print("RAG_EVAL_METRICS " + json.dumps(metrics, ensure_ascii=False, sort_keys=True))

    assert all(candidate == metrics for candidate in metrics_runs[1:])
    assert metrics["case_count"] == len(cases)
    assert metrics["k"] == 5
    for key in ("recall_at_k", "mrr_at_k", "hit_rate_at_k"):
        assert 0.0 <= metrics[key] <= 1.0
