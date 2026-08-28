from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from mcp.client import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.server.mcpserver.exceptions import ToolError

from app.db.models import Collection, CollectionItem
from app.mcp.server import MCPKnowledgeServer, _bounded_result

from conftest import wait_for_job


async def _run_client_call(provider: MCPKnowledgeServer, name: str, arguments: dict):
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
            with pytest.raises(asyncio.CancelledError):
                await server_task


def _result_payload(result) -> dict:
    assert not result.is_error, result
    if result.structured_content is not None:
        return result.structured_content
    text = result.content[0].text
    return json.loads(text)


def _provider(client) -> MCPKnowledgeServer:
    return MCPKnowledgeServer(client.app.state.knowledge_service)


def test_mcp_server_exposes_exactly_five_strict_tools(client) -> None:
    provider = _provider(client)

    async def list_tools():
        return await provider.server.list_tools()

    tools = client.portal.call(list_tools)
    assert [tool.name for tool in tools] == [
        "add_text",
        "add_url",
        "search_knowledge",
        "get_item",
        "list_collections",
    ]
    assert all(tool.input_schema.get("additionalProperties") is False for tool in tools)


def test_mcp_tools_reuse_shared_submit_search_item_and_collection_services(client) -> None:
    provider = _provider(client)
    added = client.portal.call(
        _run_client_call,
        provider,
        "add_text",
        {
            "content": "MCP 共享 service 必须使用 SQLite 当前版本。",
            "source_type": "markdown",
            "idempotency_key": "mcp-stage6-key",
        },
    )
    added_payload = _result_payload(added)
    assert added_payload["item_id"]
    wait_for_job(client, added_payload["job_id"])
    assert client.post(f"/api/items/{added_payload['item_id']}/review", json={}).status_code == 200
    assert client.post(f"/api/items/{added_payload['item_id']}/publish").status_code == 200

    searched = client.portal.call(
        _run_client_call,
        provider,
        "search_knowledge",
        {"query": "MCP SQLite 当前版本"},
    )
    search_payload = _result_payload(searched)
    assert search_payload["results"]
    assert search_payload["results"][0]["knowledge_item_id"] == added_payload["item_id"]

    item = client.portal.call(
        _run_client_call,
        provider,
        "get_item",
        {"item_id": added_payload["item_id"]},
    )
    item_payload = _result_payload(item)
    assert item_payload["id"] == added_payload["item_id"]
    assert "SQLite 当前版本" in item_payload["body"]

    async def insert_collection():
        async with client.app.state.session_factory() as session, session.begin():
            collection = Collection(id=str(uuid4()), name="MCP 合集", description="合成合集")
            session.add(collection)
            session.add(
                CollectionItem(
                    collection_id=collection.id,
                    knowledge_item_id=added_payload["item_id"],
                )
            )

    client.portal.call(insert_collection)
    collections = client.portal.call(
        _run_client_call,
        provider,
        "list_collections",
        {},
    )
    collection_payload = _result_payload(collections)
    assert collection_payload["collections"] == [
        {
            "id": collection_payload["collections"][0]["id"],
            "name": "MCP 合集",
            "description": "合成合集",
            "item_count": 1,
        }
    ]


def test_mcp_rejects_extra_fields_ssrf_paths_and_redacts_sensitive_values(client) -> None:
    provider = _provider(client)

    extra = client.portal.call(
        _run_client_call,
        provider,
        "add_text",
        {"content": "safe", "unexpected": "TRACEBACK_SENTINEL"},
    )
    assert extra.is_error is True
    assert "TRACEBACK_SENTINEL" not in str(extra)

    ssrf = client.portal.call(
        _run_client_call,
        provider,
        "add_url",
        {"url": "http://127.0.0.1/private", "idempotency_key": "ssrf-stage6"},
    )
    assert ssrf.is_error is True
    assert "127.0.0.1" not in str(ssrf)

    invalid_path = client.portal.call(
        _run_client_call,
        provider,
        "get_item",
        {"item_id": "C:\\Users\\Lenovo\\Vault Root\\note.md"},
    )
    assert invalid_path.is_error is True
    assert "Vault Root" not in str(invalid_path)

    invalid_id = client.portal.call(
        _run_client_call,
        provider,
        "get_item",
        {"item_id": "not-a-uuid"},
    )
    assert invalid_id.is_error is True
    assert "not-a-uuid" not in str(invalid_id)


def test_mcp_direct_sdk_call_has_stable_error_without_traceback(client) -> None:
    provider = _provider(client)

    async def invalid_call():
        with pytest.raises(ToolError) as raised:
            await provider.server.call_tool(
                "add_text", {"content": "x", "extra": "TRACEBACK_SENTINEL"}
            )
        return str(raised.value)

    message = client.portal.call(invalid_call)
    assert message == "mcp_invalid_arguments"


def test_mcp_normal_results_redact_headers_paths_but_keep_published_content(client) -> None:
    provider = _provider(client)
    content = (
        "安全正文片段。Cookie: COOKIE_SENTINEL; Set-Cookie: SESSION_SENTINEL; "
        "Authorization: Bearer AUTH_SENTINEL; api_key=API_KEY_SENTINEL; "
        "C:\\Users\\Lenovo\\Vault Root\\note.md"
    )
    added = client.portal.call(
        _run_client_call,
        provider,
        "add_text",
        {"content": content, "source_type": "markdown", "title": "安全标题"},
    )
    added_payload = _result_payload(added)
    wait_for_job(client, added_payload["job_id"])
    assert client.post(f"/api/items/{added_payload['item_id']}/review", json={}).status_code == 200
    assert client.post(f"/api/items/{added_payload['item_id']}/publish").status_code == 200

    item = client.portal.call(
        _run_client_call,
        provider,
        "get_item",
        {"item_id": added_payload["item_id"]},
    )
    item_payload = _result_payload(item)
    item_text = json.dumps(item_payload, ensure_ascii=False)
    assert "安全正文片段" in item_payload["body"]
    for sentinel in (
        "COOKIE_SENTINEL",
        "SESSION_SENTINEL",
        "AUTH_SENTINEL",
        "API_KEY_SENTINEL",
        "C:\\Users\\Lenovo",
        "Vault Root",
        "note.md",
    ):
        assert sentinel not in item_text

    searched = client.portal.call(
        _run_client_call,
        provider,
        "search_knowledge",
        {"query": "安全正文片段"},
    )
    search_payload = _result_payload(searched)
    search_text = json.dumps(search_payload, ensure_ascii=False)
    assert "安全正文片段" in search_payload["results"][0]["excerpt"]
    assert all(sentinel not in search_text for sentinel in (
        "COOKIE_SENTINEL",
        "SESSION_SENTINEL",
        "AUTH_SENTINEL",
        "API_KEY_SENTINEL",
        "C:\\Users\\Lenovo",
        "Vault Root",
        "note.md",
    ))

    async def insert_sensitive_collection():
        async with client.app.state.session_factory() as session, session.begin():
            collection = Collection(
                id=str(uuid4()),
                name="合集 Cookie: COLLECTION_COOKIE_SENTINEL",
                description="说明 Set-Cookie: COLLECTION_SESSION_SENTINEL",
            )
            session.add(collection)

    client.portal.call(insert_sensitive_collection)
    collections = client.portal.call(
        _run_client_call,
        provider,
        "list_collections",
        {},
    )
    collection_text = json.dumps(_result_payload(collections), ensure_ascii=False)
    assert "合集" in collection_text
    assert "COLLECTION_COOKIE_SENTINEL" not in collection_text
    assert "COLLECTION_SESSION_SENTINEL" not in collection_text

    normal_projection = _bounded_result(
        {
            "body": "正文 Cookie: COOKIE_DIRECT_SENTINEL",
            "description": "Set-Cookie: SESSION_DIRECT_SENTINEL",
        }
    )
    assert "正文" in normal_projection["body"]
    assert "COOKIE_DIRECT_SENTINEL" not in json.dumps(normal_projection)
    assert "SESSION_DIRECT_SENTINEL" not in json.dumps(normal_projection)


def test_mcp_get_item_rejects_unpublished_draft_without_returning_body(client) -> None:
    provider = _provider(client)
    added = client.portal.call(
        _run_client_call,
        provider,
        "add_text",
        {"content": "不可通过 MCP 读取的待审核正文", "source_type": "markdown"},
    )
    added_payload = _result_payload(added)
    wait_for_job(client, added_payload["job_id"])
    pending = client.portal.call(
        _run_client_call,
        provider,
        "get_item",
        {"item_id": added_payload["item_id"]},
    )
    assert pending.is_error is True
    assert "mcp_item_not_published" in str(pending)
    assert "不可通过 MCP 读取" not in str(pending)
