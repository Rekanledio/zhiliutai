from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from mcp.client import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from fastapi.testclient import TestClient

from app.mcp.server import MCPKnowledgeServer
from app.providers.rag import AnswerClaim, AnswerDraft

from conftest import wait_for_job


class DeterministicEndToEndRagProvider:
    provider = "stage6-e2e-fake"
    model = "stage6-e2e-v1"
    prompt_version = "stage6-e2e-test-v1"

    def __init__(self) -> None:
        self.answer_calls = 0

    async def answer(
        self, _query: str, _evidence: Sequence[Mapping[str, str]]
    ) -> AnswerDraft:
        self.answer_calls += 1
        return AnswerDraft(
            claims=(
                AnswerClaim(
                    "合成闭环的版本权威是 SQLite 当前版本。",
                    ("C1",),
                ),
            )
        )

    async def rewrite_query(self, query: str) -> str:
        return query


async def _call_mcp_tool(
    provider: MCPKnowledgeServer,
    name: str,
    arguments: dict[str, object],
):
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_task = asyncio.create_task(
            provider.server._lowlevel_server.run(
                *server_streams,
                provider.server._lowlevel_server.create_initialization_options(),
            )
        )
        try:
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                return await session.call_tool(name, arguments)
        finally:
            if not server_task.done():
                server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass


def _mcp_payload(result) -> dict[str, object]:
    assert not result.is_error, result
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_synthetic_closed_loop_reaches_obsidian_search_answer_and_mcp(
    client: TestClient,
) -> None:
    content = "SQLite 是合成闭环的唯一版本权威。Obsidian Markdown 是确认后的正文主来源。"
    submitted = client.post(
        "/api/sources/text",
        json={"content": content, "source_type": "markdown", "title": "合成闭环"},
    )
    assert submitted.status_code == 202, submitted.text
    submission = submitted.json()
    wait_for_job(client, submission["job_id"])

    pending = client.get(f"/api/items/{submission['item_id']}")
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending_review"

    reviewed = client.post(f"/api/items/{submission['item_id']}/review", json={})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "reviewed"

    published = client.post(f"/api/items/{submission['item_id']}/publish")
    assert published.status_code == 200, published.text
    published_item = published.json()
    assert published_item["status"] == "published"
    assert published_item["current_content_version_id"]
    assert published_item["pending_content_version_id"] is None

    note_relative_path = published_item["note_relative_path"]
    assert isinstance(note_relative_path, str)
    note_path = client.app.state.settings.managed_vault_root / Path(note_relative_path)
    assert note_path.is_file()
    note_text = note_path.read_text(encoding="utf-8")
    assert f'zhiliu_id: "{submission["item_id"]}"' in note_text
    assert content in note_text

    search = client.post("/api/search", json={"query": "SQLite"})
    assert search.status_code == 200, search.text
    search_payload = search.json()
    assert search_payload["evidence"]["status"] == "sufficient"
    assert search_payload["results"][0]["knowledge_item_id"] == submission["item_id"]
    citation = search_payload["results"][0]["citation"]
    assert citation["knowledge_item_id"] == submission["item_id"]
    assert citation["content_version_id"] == published_item["current_content_version_id"]

    rag_provider = DeterministicEndToEndRagProvider()
    client.app.state.question_answer_service.chat_provider = rag_provider
    answer = client.post("/api/chat/stream", json={"query": "SQLite"})
    assert answer.status_code == 200, answer.text
    assert [
        line.removeprefix("event: ")
        for line in answer.text.splitlines()
        if line.startswith("event:")
    ] == ["meta", "delta", "citations", "done"]
    assert "SQLite 当前版本" in answer.text
    assert "C1" in answer.text
    assert rag_provider.answer_calls == 1

    provider = MCPKnowledgeServer(client.app.state.knowledge_service)
    mcp_search = client.portal.call(
        _call_mcp_tool,
        provider,
        "search_knowledge",
        {"query": "SQLite"},
    )
    mcp_payload = _mcp_payload(mcp_search)
    assert mcp_payload["evidence"]["status"] == "sufficient"
    assert mcp_payload["results"][0]["knowledge_item_id"] == submission["item_id"]

    mcp_item = client.portal.call(
        _call_mcp_tool,
        provider,
        "get_item",
        {"item_id": submission["item_id"]},
    )
    mcp_item_payload = _mcp_payload(mcp_item)
    assert mcp_item_payload["status"] == "published"
    assert mcp_item_payload["note_relative_path"] == note_relative_path
