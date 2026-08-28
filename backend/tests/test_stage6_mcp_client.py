from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any

import pytest
from mcp.server import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import Tool
from pydantic import ValidationError

from app.mcp.client import (
    ControlledMCPClient,
    MCPClientConfiguration,
    MCPClientError,
    MCPServerProfile,
    load_mcp_client_configuration,
)
from app.mcp.server import MCPKnowledgeServer


def _profile(
    *,
    expected_name: str = "controlled",
    allowed_tools: list[str] | None = None,
    call_timeout: float = 1.0,
    max_response_bytes: int = 200_000,
    entrypoint_id: str = "test",
) -> MCPServerProfile:
    return MCPServerProfile(
        server_id="test-server",
        expected_server_name=expected_name,
        entrypoint_id=entrypoint_id,
        allowed_tools=allowed_tools or ["echo"],
        connect_timeout_seconds=1.0,
        call_timeout_seconds=call_timeout,
        max_response_bytes=max_response_bytes,
    )


def _memory_factory(server: MCPServer, *, task_sink: list[asyncio.Task[Any]] | None = None):
    @asynccontextmanager
    async def transport(_profile: MCPServerProfile):
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            server_task = asyncio.create_task(
                server._lowlevel_server.run(
                    *server_streams,
                    server._lowlevel_server.create_initialization_options(),
                )
            )
            if task_sink is not None:
                task_sink.append(server_task)
            try:
                yield client_streams
            finally:
                if not server_task.done():
                    server_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await server_task

    return transport


class _StrictToolSchemaServer(MCPServer):
    async def list_tools(self):
        tools = await super().list_tools()
        return [
            tool.model_copy(
                update={
                    "input_schema": {
                        **tool.input_schema,
                        "additionalProperties": False,
                    }
                }
            )
            for tool in tools
        ]


class _StaticToolSchemaServer(MCPServer):
    def __init__(self, tools: list[Tool]) -> None:
        super().__init__(name="controlled", version="test-v1")
        self._static_tools = tools

    async def list_tools(self):
        return self._static_tools


def _echo_server(*, calls: list[dict[str, Any]] | None = None, delay: float = 0) -> MCPServer:
    server = _StrictToolSchemaServer(name="controlled", version="test-v1")

    async def echo(value: str) -> dict[str, str]:
        if calls is not None:
            calls.append({"value": value})
        if delay:
            await asyncio.sleep(delay)
        return {"value": value}

    server.add_tool(echo, name="echo", description="deterministic echo")
    return server


def test_client_configuration_is_strict_and_never_accepts_raw_command_or_endpoint() -> None:
    profile = _profile()
    config = MCPClientConfiguration(servers=[profile])
    assert config.server("test-server") is profile

    with pytest.raises(MCPClientError) as unknown:
        config.server("not-configured")
    assert str(unknown.value) == "mcp_client_server_not_configured"

    with pytest.raises(ValidationError):
        MCPServerProfile.model_validate(
            {
                **profile.model_dump(),
                "command": "powershell -c Get-ChildItem",
            }
        )
    with pytest.raises(ValidationError):
        MCPServerProfile.model_validate({**profile.model_dump(), "endpoint": "http://127.0.0.1"})
    with pytest.raises(ValidationError):
        MCPServerProfile.model_validate({**profile.model_dump(), "transport": "streamable_http"})
    with pytest.raises(MCPClientError) as malformed:
        load_mcp_client_configuration('{"servers": ["API_KEY_SENTINEL"]')
    assert "API_KEY_SENTINEL" not in str(malformed.value)


@pytest.mark.asyncio
async def test_client_handshake_and_capability_intersection_reject_unknown_calls() -> None:
    calls: list[dict[str, Any]] = []
    server = _echo_server(calls=calls)

    async with ControlledMCPClient(
        _profile(allowed_tools=["echo", "not_advertised"]),
        transport_factory=_memory_factory(server),
    ) as client:
        assert client.connected is True
        assert client.available_tools() == ("echo",)
        result = await client.call_tool("echo", {"value": "hello"})
        assert result["structuredContent"] == {"value": "hello"}
        with pytest.raises(MCPClientError) as unknown:
            await client.call_tool("not_advertised", {})
        assert unknown.value.code == "unknown_tool"

    assert client.connected is False
    assert calls == [{"value": "hello"}]


@pytest.mark.asyncio
async def test_client_accepts_the_stage6_provider_strict_tool_schemas() -> None:
    provider = MCPKnowledgeServer(object(), name="zhiliutai")
    profile = _profile(
        expected_name="zhiliutai",
        allowed_tools=[
            "add_text",
            "add_url",
            "search_knowledge",
            "get_item",
            "list_collections",
        ],
    )
    async with ControlledMCPClient(
        profile,
        transport_factory=_memory_factory(provider.server),
    ) as client:
        assert client.available_tools() == (
            "add_text",
            "add_url",
            "get_item",
            "list_collections",
            "search_knowledge",
        )


@pytest.mark.asyncio
async def test_client_rejects_argument_injection_before_server_and_redacts_result() -> None:
    calls: list[dict[str, Any]] = []
    server = _echo_server(calls=calls)
    client = ControlledMCPClient(
        _profile(),
        transport_factory=_memory_factory(server),
    )
    async with client:
        with pytest.raises(MCPClientError) as invalid:
            await client.call_tool(
                "echo",
                {"value": "safe", "unexpected": "TRACEBACK_SENTINEL"},
            )
        assert invalid.value.code == "invalid_arguments"
        assert calls == []

    leaking_server = _StrictToolSchemaServer(name="controlled", version="test-v1")

    async def leaking(value: str) -> dict[str, str]:
        return {
            "value": value,
            "payload": "api_key=API_KEY_SENTINEL",
            "authorization": "AUTHORIZATION_SENTINEL",
            "cookie": "COOKIE_SENTINEL",
            "traceback": "TRACEBACK_SENTINEL",
        }

    leaking_server.add_tool(leaking, name="echo")
    async with ControlledMCPClient(
        _profile(),
        transport_factory=_memory_factory(leaking_server),
    ) as client:
        result = await client.call_tool("echo", {"value": "safe"})
    encoded = str(result)
    assert "API_KEY_SENTINEL" not in encoded
    assert "AUTHORIZATION_SENTINEL" not in encoded
    assert "COOKIE_SENTINEL" not in encoded
    assert "TRACEBACK_SENTINEL" not in encoded


@pytest.mark.asyncio
async def test_client_rejects_malicious_schema_and_server_identity() -> None:
    malicious = _StaticToolSchemaServer(
        [
            Tool(
                name="echo",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": True,
                },
            )
        ]
    )
    with pytest.raises(MCPClientError) as schema_error:
        async with ControlledMCPClient(
            _profile(),
            transport_factory=_memory_factory(malicious),
        ):
            pass
    assert schema_error.value.code == "malicious_schema"

    other = _echo_server()
    with pytest.raises(MCPClientError) as identity_error:
        async with ControlledMCPClient(
            _profile(expected_name="not-controlled"),
            transport_factory=_memory_factory(other),
        ):
            pass
    assert identity_error.value.code == "server_identity_mismatch"


@pytest.mark.asyncio
async def test_client_timeout_and_response_limit_are_stable_and_close_cleanly() -> None:
    timed_server = _echo_server(delay=0.2)
    async with ControlledMCPClient(
        _profile(call_timeout=0.05),
        transport_factory=_memory_factory(timed_server),
    ) as client:
        with pytest.raises(MCPClientError) as timed_out:
            await client.call_tool("echo", {"value": "slow"})
        assert timed_out.value.code == "timeout"

    large_server = _StrictToolSchemaServer(name="controlled", version="test-v1")

    async def large(value: str) -> dict[str, str]:
        return {"value": value * 2_000}

    large_server.add_tool(large, name="echo")
    async with ControlledMCPClient(
        _profile(max_response_bytes=1_024),
        transport_factory=_memory_factory(large_server),
    ) as client:
        with pytest.raises(MCPClientError) as too_large:
            await client.call_tool("echo", {"value": "x"})
        assert too_large.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_client_disconnect_and_remote_error_are_stable() -> None:
    server_tasks: list[asyncio.Task[Any]] = []
    server = _echo_server()
    async with ControlledMCPClient(
        _profile(call_timeout=0.2),
        transport_factory=_memory_factory(server, task_sink=server_tasks),
    ) as client:
        server_tasks[0].cancel()
        with suppress(asyncio.CancelledError):
            await server_tasks[0]
        with pytest.raises(MCPClientError) as disconnected:
            await client.call_tool("echo", {"value": "after-disconnect"})
        assert disconnected.value.code in {"disconnected", "timeout"}

    failing_server = _StrictToolSchemaServer(name="controlled", version="test-v1")

    async def fail(value: str) -> str:
        del value
        raise RuntimeError("TRACEBACK_SENTINEL")

    failing_server.add_tool(fail, name="echo")
    async with ControlledMCPClient(
        _profile(),
        transport_factory=_memory_factory(failing_server),
    ) as client:
        with pytest.raises(MCPClientError) as failed:
            await client.call_tool("echo", {"value": "fail"})
        assert failed.value.code == "tool_failed"
        assert "TRACEBACK_SENTINEL" not in str(failed.value)


@pytest.mark.asyncio
async def test_client_cancellation_and_unconfigured_stdio_entrypoint_fail_closed() -> None:
    server = _echo_server(delay=0.2)
    client = ControlledMCPClient(
        _profile(call_timeout=1.0),
        transport_factory=_memory_factory(server),
    )
    async with client:
        pending = asyncio.create_task(client.call_tool("echo", {"value": "cancel"}))
        await asyncio.sleep(0.02)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    with pytest.raises(MCPClientError) as unconfigured:
        async with ControlledMCPClient(_profile(entrypoint_id="not-registered")):
            pass
    assert unconfigured.value.code == "server_not_configured"
