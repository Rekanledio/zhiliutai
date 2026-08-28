"""Fail-closed MCP Consumer boundary for explicitly trusted server profiles.

The client deliberately supports only stdio in this batch. A profile names a
trusted application entrypoint; it never carries a shell command, arguments,
working directory, endpoint, or secret. Test transports may be injected by
application code, but they are not configurable through the Pydantic boundary.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias

from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import PaginatedRequestParams
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.config import PROJECT_ROOT
from app.core.safety import REDACTED, redact_sensitive_text


_SAFE_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SAFE_ENTRYPOINT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_COOKIE_TEXT = re.compile(r"(?i)\b(?:cookie|set-cookie)\s*[:=]\s*[^\s,;]+")
_UNSAFE_TEXT = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer|cookie|secret|password|traceback|"
    r"access[_ -]?token|refresh[_ -]?token)"
)

_CLIENT_ERROR_CODES = frozenset(
    {
        "invalid_configuration",
        "server_not_configured",
        "unsupported_transport",
        "connection_timeout",
        "connection_failed",
        "server_identity_mismatch",
        "tools_unavailable",
        "malicious_schema",
        "capability_not_allowed",
        "unknown_tool",
        "not_connected",
        "invalid_arguments",
        "request_too_large",
        "timeout",
        "disconnected",
        "tool_failed",
        "result_invalid",
        "response_too_large",
        "close_failed",
    }
)

_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "title",
        "description",
        "default",
    }
)
_SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_SENSITIVE_RESULT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "set_cookie",
        "headers",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "traceback",
        "response",
        "response_body",
        "upstream_response",
        "command",
        "args",
        "cwd",
        "path",
        "file_path",
        "local_path",
        "vault_path",
    }
)
_MAX_SCHEMA_BYTES = 100_000
_MAX_SCHEMA_DEPTH = 8
_MAX_SCHEMA_PROPERTIES = 64
_MAX_SCHEMA_ENUM_VALUES = 100
_MAX_SCHEMA_STRING_BOUND = 1_000_000
_MAX_SCHEMA_COLLECTION_ITEMS = 1_000
_MAX_TOOL_PAGES = 8
_MAX_ADVERTISED_TOOLS = 128
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_ITEMS = 1_000
_MAX_RESULT_STRING_CHARS = 1_000_000


class MCPClientError(Exception):
    """An MCP client failure with a stable, non-sensitive public code."""

    def __init__(self, code: str) -> None:
        safe_code = code if code in _CLIENT_ERROR_CODES and _SAFE_ERROR_CODE.fullmatch(code) else "connection_failed"
        self.code = safe_code
        super().__init__(f"mcp_client_{safe_code}")


class MCPServerProfile(BaseModel):
    """User-selectable metadata, never an executable command or raw endpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    server_id: StrictStr = Field(min_length=1, max_length=64)
    expected_server_name: StrictStr = Field(min_length=1, max_length=128)
    transport: Literal["stdio"] = "stdio"
    endpoint: None = None
    entrypoint_id: StrictStr = Field(min_length=1, max_length=64)
    allowed_tools: list[StrictStr] = Field(min_length=1, max_length=32)
    connect_timeout_seconds: StrictFloat = Field(default=10.0, gt=0, le=30.0)
    call_timeout_seconds: StrictFloat = Field(default=30.0, gt=0, le=60.0)
    max_request_bytes: StrictInt = Field(default=100_000, ge=1_024, le=200_000)
    max_response_bytes: StrictInt = Field(default=200_000, ge=1_024, le=200_000)

    @field_validator("server_id")
    @classmethod
    def validate_server_id(cls, value: str) -> str:
        if not _SAFE_SERVER_ID.fullmatch(value):
            raise ValueError("invalid server id")
        return value

    @field_validator("entrypoint_id")
    @classmethod
    def validate_entrypoint_id(cls, value: str) -> str:
        if not _SAFE_ENTRYPOINT_ID.fullmatch(value):
            raise ValueError("invalid entrypoint id")
        return value

    @field_validator("expected_server_name")
    @classmethod
    def validate_server_name(cls, value: str) -> str:
        if any(ord(character) < 0x20 for character in value):
            raise ValueError("invalid server name")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def validate_allowed_tools(cls, values: list[str]) -> list[str]:
        if not values or len(set(values)) != len(values):
            raise ValueError("allowed tools must be unique")
        if any(not _SAFE_TOOL_NAME.fullmatch(value) for value in values):
            raise ValueError("invalid tool name")
        return values

    @model_validator(mode="after")
    def reject_unimplemented_transports(self) -> MCPServerProfile:
        if self.transport != "stdio":
            raise ValueError("unsupported transport")
        if self.endpoint is not None:
            raise ValueError("endpoint is not accepted for stdio")
        return self


class MCPClientConfiguration(BaseModel):
    """A structured allowlist; no connection is attempted merely by loading it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    servers: list[MCPServerProfile] = Field(default_factory=list, max_length=16)

    @field_validator("servers")
    @classmethod
    def validate_unique_servers(cls, values: list[MCPServerProfile]) -> list[MCPServerProfile]:
        server_ids = [server.server_id for server in values]
        if len(set(server_ids)) != len(server_ids):
            raise ValueError("server ids must be unique")
        return values

    def server(self, server_id: str) -> MCPServerProfile:
        if not isinstance(server_id, str) or not _SAFE_SERVER_ID.fullmatch(server_id):
            raise MCPClientError("server_not_configured")
        for profile in self.servers:
            if profile.server_id == server_id:
                return profile
        raise MCPClientError("server_not_configured")


def load_mcp_client_configuration(raw: str) -> MCPClientConfiguration:
    """Parse explicit JSON configuration without echoing raw input on failure."""

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 100_000:
        raise MCPClientError("invalid_configuration")
    try:
        value = json.loads(raw)
        return MCPClientConfiguration.model_validate(value)
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
        raise MCPClientError("invalid_configuration") from error


@dataclass(frozen=True)
class TrustedStdioEntrypoint:
    """An executable entrypoint supplied by trusted application code, not config."""

    command: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None

    def parameters(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=self.command,
            args=list(self.args),
            cwd=self.cwd,
        )


_DEFAULT_STDIO_ENTRYPOINTS: Mapping[str, TrustedStdioEntrypoint] = {
    "zhiliutai": TrustedStdioEntrypoint(
        command=sys.executable,
        args=("-m", "app.mcp.server"),
        cwd=PROJECT_ROOT / "backend",
    )
}

StreamPair: TypeAlias = tuple[Any, Any]
TransportFactory: TypeAlias = Callable[
    [MCPServerProfile], AbstractAsyncContextManager[StreamPair]
]


def _client_error(code: str) -> MCPClientError:
    return MCPClientError(code)


def _json_bytes(value: object, *, limit: int, error_code: str) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise _client_error(error_code) from error
    if len(encoded) > limit:
        raise _client_error(error_code)
    return encoded


def _assert_json_value(value: object, *, depth: int = 0) -> None:
    if depth > _MAX_RESULT_DEPTH:
        raise _client_error("invalid_arguments")
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise _client_error("invalid_arguments")
        if isinstance(value, str) and (len(value) > _MAX_RESULT_STRING_CHARS or "\x00" in value):
            raise _client_error("invalid_arguments")
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_RESULT_ITEMS:
            raise _client_error("invalid_arguments")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128 or "\x00" in key:
                raise _client_error("invalid_arguments")
            _assert_json_value(item, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _MAX_RESULT_ITEMS:
            raise _client_error("invalid_arguments")
        for item in value:
            _assert_json_value(item, depth=depth + 1)
        return
    raise _client_error("invalid_arguments")


def _schema_integer(value: object, *, maximum: int) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise _client_error("malicious_schema")
    return value


def _schema_number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _client_error("malicious_schema")
    if isinstance(value, float) and not math.isfinite(value):
        raise _client_error("malicious_schema")
    return value


def _validate_schema(schema: object, *, depth: int = 0) -> None:
    if depth > _MAX_SCHEMA_DEPTH or not isinstance(schema, Mapping):
        raise _client_error("malicious_schema")
    _json_bytes(dict(schema), limit=_MAX_SCHEMA_BYTES, error_code="malicious_schema")
    if any(not isinstance(key, str) or key not in _SCHEMA_KEYS for key in schema):
        raise _client_error("malicious_schema")

    schema_type = schema.get("type")
    any_of = schema.get("anyOf")
    if schema_type is None and any_of is None:
        raise _client_error("malicious_schema")
    if schema_type is not None and (not isinstance(schema_type, str) or schema_type not in _SCHEMA_TYPES):
        raise _client_error("malicious_schema")

    if any_of is not None:
        if not isinstance(any_of, list) or not 1 <= len(any_of) <= 4:
            raise _client_error("malicious_schema")
        for branch in any_of:
            _validate_schema(branch, depth=depth + 1)

    if "properties" in schema:
        if schema_type != "object" or not isinstance(schema["properties"], Mapping):
            raise _client_error("malicious_schema")
        if len(schema["properties"]) > _MAX_SCHEMA_PROPERTIES:
            raise _client_error("malicious_schema")
        for key, child in schema["properties"].items():
            if not isinstance(key, str) or not _SAFE_TOOL_NAME.fullmatch(key):
                raise _client_error("malicious_schema")
            _validate_schema(child, depth=depth + 1)

    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            raise _client_error("malicious_schema")
        required = schema.get("required", [])
        if not isinstance(required, list) or len(required) > _MAX_SCHEMA_PROPERTIES:
            raise _client_error("malicious_schema")
        if len(set(required)) != len(required) or any(
            not isinstance(key, str) or key not in schema.get("properties", {}) for key in required
        ):
            raise _client_error("malicious_schema")
    elif "additionalProperties" in schema or "required" in schema:
        raise _client_error("malicious_schema")

    if schema_type == "array":
        if not isinstance(schema.get("items"), Mapping):
            raise _client_error("malicious_schema")
        _validate_schema(schema["items"], depth=depth + 1)
    elif "items" in schema:
        raise _client_error("malicious_schema")

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not 1 <= len(enum) <= _MAX_SCHEMA_ENUM_VALUES:
            raise _client_error("malicious_schema")
        _assert_json_value(enum)
    if "const" in schema:
        _assert_json_value(schema["const"])

    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            bound = _schema_integer(schema[key], maximum=_MAX_SCHEMA_STRING_BOUND)
            if key.startswith("max") and bound > _MAX_SCHEMA_STRING_BOUND:
                raise _client_error("malicious_schema")
    for key in ("minimum", "maximum"):
        if key in schema:
            _schema_number(schema[key])

    if schema_type not in {"string", "array"} and any(
        key in schema for key in ("minLength", "maxLength", "minItems", "maxItems")
    ):
        raise _client_error("malicious_schema")
    for key in ("title", "description"):
        if key in schema and (not isinstance(schema[key], str) or len(schema[key]) > 1_000):
            raise _client_error("malicious_schema")
    if "default" in schema:
        _assert_json_value(schema["default"])


def _matches_schema(value: object, schema: Mapping[str, Any]) -> bool:
    any_of = schema.get("anyOf")
    if any_of is not None:
        return any(_matches_schema(value, branch) for branch in any_of if isinstance(branch, Mapping))

    schema_type = schema.get("type")
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return type(value) is bool
    if schema_type == "string":
        if not isinstance(value, str):
            return False
        if "minLength" in schema and len(value) < schema["minLength"]:
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
    elif schema_type == "integer":
        if type(value) is not int:
            return False
    elif schema_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
    elif schema_type == "array":
        if not isinstance(value, list):
            return False
        if "minItems" in schema and len(value) < schema["minItems"]:
            return False
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return False
        items = schema.get("items")
        if not isinstance(items, Mapping) or not all(_matches_schema(item, items) for item in value):
            return False
    elif schema_type == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            return False
        required = schema.get("required", [])
        if any(key not in value for key in required):
            return False
        if any(key not in properties for key in value):
            return False
        if not all(_matches_schema(value[key], properties[key]) for key in value):
            return False
    else:
        return False

    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        return False
    if "maximum" in schema and isinstance(value, (int, float)) and value > schema["maximum"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "const" in schema and value != schema["const"]:
        return False
    return True


def _validate_arguments(schema: Mapping[str, Any], arguments: object, *, max_bytes: int) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise _client_error("invalid_arguments")
    _assert_json_value(arguments)
    _json_bytes(arguments, limit=max_bytes, error_code="request_too_large")
    if not _matches_schema(arguments, schema):
        raise _client_error("invalid_arguments")
    return arguments


def _sanitize_result(value: object, *, depth: int = 0) -> object:
    if depth > _MAX_RESULT_DEPTH:
        raise _client_error("result_invalid")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _client_error("result_invalid")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_RESULT_STRING_CHARS or "\x00" in value:
            raise _client_error("result_invalid")
        redacted = _COOKIE_TEXT.sub(REDACTED, redact_sensitive_text(value))
        return REDACTED if _UNSAFE_TEXT.search(redacted) else redacted
    if isinstance(value, Mapping):
        if len(value) > _MAX_RESULT_ITEMS:
            raise _client_error("result_invalid")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128 or "\x00" in key:
                raise _client_error("result_invalid")
            normalized = key.casefold().replace("-", "_")
            result[key] = (
                REDACTED
                if normalized in _SENSITIVE_RESULT_KEYS
                else _sanitize_result(item, depth=depth + 1)
            )
        return result
    if isinstance(value, list):
        if len(value) > _MAX_RESULT_ITEMS:
            raise _client_error("result_invalid")
        return [_sanitize_result(item, depth=depth + 1) for item in value]
    raise _client_error("result_invalid")


def _project_result(result: object, *, max_bytes: int) -> dict[str, object]:
    if bool(getattr(result, "is_error", False)):
        raise _client_error("tool_failed")
    if getattr(result, "result_type", "complete") != "complete":
        raise _client_error("result_invalid")
    model_dump = getattr(result, "model_dump", None)
    if not callable(model_dump):
        raise _client_error("result_invalid")
    try:
        dumped = model_dump(mode="json", by_alias=True, exclude_none=True)
    except (TypeError, ValueError) as error:
        raise _client_error("result_invalid") from error
    safe = _sanitize_result(dumped)
    if not isinstance(safe, dict):
        raise _client_error("result_invalid")
    _json_bytes(safe, limit=max_bytes, error_code="response_too_large")
    return safe


class ControlledMCPClient:
    """One explicitly selected MCP server connection with no autonomous tool loop."""

    def __init__(
        self,
        profile: MCPServerProfile,
        *,
        transport_factory: TransportFactory | None = None,
        trusted_stdio_entrypoints: Mapping[str, TrustedStdioEntrypoint] | None = None,
    ) -> None:
        self.profile = profile
        self._transport_factory = transport_factory
        self._entrypoints = dict(_DEFAULT_STDIO_ENTRYPOINTS)
        if trusted_stdio_entrypoints:
            self._entrypoints.update(trusted_stdio_entrypoints)
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: dict[str, dict[str, Any]] = {}

    @classmethod
    def for_server(
        cls,
        configuration: MCPClientConfiguration,
        server_id: str,
        *,
        transport_factory: TransportFactory | None = None,
        trusted_stdio_entrypoints: Mapping[str, TrustedStdioEntrypoint] | None = None,
    ) -> ControlledMCPClient:
        return cls(
            configuration.server(server_id),
            transport_factory=transport_factory,
            trusted_stdio_entrypoints=trusted_stdio_entrypoints,
        )

    def _transport(self) -> AbstractAsyncContextManager[StreamPair]:
        if self._transport_factory is not None:
            try:
                return self._transport_factory(self.profile)
            except MCPClientError:
                raise
            except Exception as error:
                raise _client_error("connection_failed") from error
        entrypoint = self._entrypoints.get(self.profile.entrypoint_id)
        if entrypoint is None:
            raise _client_error("server_not_configured")
        return stdio_client(entrypoint.parameters())

    async def __aenter__(self) -> ControlledMCPClient:
        if self._stack is not None:
            raise _client_error("connection_failed")
        stack = AsyncExitStack()
        try:
            streams = await stack.enter_async_context(self._transport())
            if not isinstance(streams, tuple) or len(streams) != 2:
                raise _client_error("connection_failed")
            session = await stack.enter_async_context(
                ClientSession(
                    streams[0],
                    streams[1],
                    read_timeout_seconds=self.profile.call_timeout_seconds,
                )
            )
            initialize = await asyncio.wait_for(
                session.initialize(), timeout=self.profile.connect_timeout_seconds
            )
            server_info = getattr(initialize, "server_info", None)
            if server_info is None or getattr(server_info, "name", None) != self.profile.expected_server_name:
                raise _client_error("server_identity_mismatch")
            tools = await asyncio.wait_for(
                self._load_tools(session), timeout=self.profile.connect_timeout_seconds
            )
            self._stack = stack
            self._session = session
            self._tools = tools
            return self
        except asyncio.CancelledError:
            await stack.aclose()
            raise
        except MCPClientError:
            await stack.aclose()
            raise
        except asyncio.TimeoutError as error:
            await stack.aclose()
            raise _client_error("connection_timeout") from error
        except Exception as error:
            await stack.aclose()
            raise _client_error("connection_failed") from error

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        stack = self._stack
        self._stack = None
        self._session = None
        self._tools = {}
        if stack is None:
            return False
        try:
            await stack.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if exc_type is None:
                raise _client_error("close_failed") from error
        return False

    async def _load_tools(self, session: ClientSession) -> dict[str, dict[str, Any]]:
        collected: list[Any] = []
        cursor: str | None = None
        for page_index in range(_MAX_TOOL_PAGES):
            params = None if cursor is None else PaginatedRequestParams(cursor=cursor)
            result = await session.list_tools(params=params)
            page_tools = getattr(result, "tools", None)
            if not isinstance(page_tools, list):
                raise _client_error("tools_unavailable")
            collected.extend(page_tools)
            if len(collected) > _MAX_ADVERTISED_TOOLS:
                raise _client_error("tools_unavailable")
            cursor = getattr(result, "next_cursor", None)
            if cursor is None:
                break
            if not isinstance(cursor, str) or not 0 < len(cursor) <= 512 or any(
                ord(character) < 0x20 for character in cursor
            ):
                raise _client_error("tools_unavailable")
        else:
            raise _client_error("tools_unavailable")

        raw = [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in collected
            if callable(getattr(tool, "model_dump", None))
        ]
        _json_bytes(raw, limit=self.profile.max_response_bytes, error_code="tools_unavailable")

        indexed: dict[str, dict[str, Any]] = {}
        for tool in collected:
            name = getattr(tool, "name", None)
            if not isinstance(name, str) or not _SAFE_TOOL_NAME.fullmatch(name):
                raise _client_error("tools_unavailable")
            if name in indexed:
                raise _client_error("tools_unavailable")
            if name not in self.profile.allowed_tools:
                continue
            schema = getattr(tool, "input_schema", None)
            _validate_schema(schema)
            indexed[name] = dict(schema)
        if not indexed:
            raise _client_error("capability_not_allowed")
        return indexed

    @property
    def connected(self) -> bool:
        return self._session is not None

    def available_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    async def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, object]:
        session = self._session
        if session is None:
            raise _client_error("not_connected")
        if not isinstance(name, str) or name not in self._tools:
            raise _client_error("unknown_tool")
        safe_arguments = _validate_arguments(
            self._tools[name],
            {} if arguments is None else arguments,
            max_bytes=self.profile.max_request_bytes,
        )
        try:
            result = await asyncio.wait_for(
                session.call_tool(
                    name,
                    safe_arguments,
                    read_timeout_seconds=self.profile.call_timeout_seconds,
                ),
                timeout=self.profile.call_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as error:
            raise _client_error("timeout") from error
        except (BrokenPipeError, ConnectionError, EOFError) as error:
            raise _client_error("disconnected") from error
        except Exception as error:
            raise _client_error("disconnected") from error
        return _project_result(result, max_bytes=self.profile.max_response_bytes)
