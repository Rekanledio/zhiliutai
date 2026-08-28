"""The controlled stdio MCP Provider with exactly five knowledge tools."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from app.core.errors import ApplicationError
from app.core.safety import REDACTED, redact_sensitive_text
from app.mcp.schemas import (
    AddTextInput,
    AddUrlInput,
    GetItemInput,
    ListCollectionsInput,
    SearchKnowledgeInput,
)
from app.rag.citations import CitationBuildError
from app.rag.retrieval import RetrievalError
from app.services.knowledge import KnowledgeApplicationService


MCP_TOOL_NAMES = (
    "add_text",
    "add_url",
    "search_knowledge",
    "get_item",
    "list_collections",
)
MAX_MCP_RESPONSE_BYTES = 200_000
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_MCP_SENSITIVE_KEYS = {
    "input",
    "raw_input",
    "request_body",
    "response",
    "response_body",
    "upstream_response",
    "headers",
    "authorization",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "cookie",
    "cookies",
    "set_cookie",
}


def _tool_error(code: str) -> ToolError:
    safe_code = code if _SAFE_ERROR_CODE.fullmatch(code) else "internal_error"
    return ToolError(f"mcp_{safe_code}")


def _map_error(error: BaseException) -> ToolError:
    if isinstance(error, ApplicationError):
        return _tool_error(error.code)
    if isinstance(error, (ValueError, ValidationError)):
        return _tool_error("invalid_arguments")
    if isinstance(error, RetrievalError):
        return _tool_error("retrieval_unavailable")
    if isinstance(error, CitationBuildError):
        return _tool_error("knowledge_changed")
    return _tool_error("internal_error")


def _sanitize_mcp_value(value: object, *, depth: int = 0) -> object:
    """Sanitize normal MCP output without erasing allowed knowledge ``body``."""

    if depth > 12:
        return REDACTED
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                result[REDACTED] = REDACTED
                continue
            normalized = key.casefold().replace("-", "_")
            result[key] = (
                REDACTED
                if normalized in _MCP_SENSITIVE_KEYS
                else _sanitize_mcp_value(item, depth=depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_mcp_value(item, depth=depth + 1) for item in value]
    if isinstance(value, bytes):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive_text(str(value))


def _bounded_result(value: Mapping[str, object]) -> dict[str, object]:
    safe_value = _sanitize_mcp_value(value)
    if not isinstance(safe_value, dict):
        raise _tool_error("internal_error")
    try:
        encoded = json.dumps(
            safe_value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise _tool_error("internal_error") from error
    if len(encoded.encode("utf-8")) > MAX_MCP_RESPONSE_BYTES:
        raise _tool_error("response_too_large")
    return safe_value


class StrictMCPServer(MCPServer):
    """MCPServer with strict raw-argument validation before SDK coercion."""

    def __init__(
        self,
        *args: Any,
        input_models: Mapping[str, type[BaseModel]],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._input_models = dict(input_models)

    async def list_tools(self):
        tools = await super().list_tools()
        output = []
        for tool in tools:
            model = self._input_models.get(tool.name)
            if model is None:
                output.append(tool)
                continue
            output.append(
                tool.model_copy(update={"input_schema": model.model_json_schema(mode="serialization")})
            )
        return output

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ):
        model = self._input_models.get(name)
        if model is not None:
            if not isinstance(arguments, dict):
                raise _tool_error("invalid_arguments")
            try:
                validated = model.model_validate(arguments)
            except ValidationError as error:
                raise _tool_error("invalid_arguments") from error
            arguments = validated.model_dump(mode="python")
        return await super().call_tool(name, arguments, context=context)


class MCPKnowledgeServer:
    """Own one SDK server and expose only the provider tool allowlist."""

    def __init__(self, knowledge: KnowledgeApplicationService, *, name: str = "zhiliutai") -> None:
        self.knowledge = knowledge
        self.server = StrictMCPServer(
            name=name,
            version="stage6-d",
            instructions="只提供受控知识服务工具；工具不执行任意命令或本地文件访问。",
            input_models={
                "add_text": AddTextInput,
                "add_url": AddUrlInput,
                "search_knowledge": SearchKnowledgeInput,
                "get_item": GetItemInput,
                "list_collections": ListCollectionsInput,
            },
        )
        self.server.add_tool(self._add_text, name="add_text", description="提交文本来源")
        self.server.add_tool(self._add_url, name="add_url", description="提交网页来源 URL")
        self.server.add_tool(
            self._search_knowledge,
            name="search_knowledge",
            description="检索已发布知识并返回受控引用",
        )
        self.server.add_tool(self._get_item, name="get_item", description="读取一个已授权知识条目")
        self.server.add_tool(
            self._list_collections,
            name="list_collections",
            description="列出人工维护的知识合集",
        )

    async def _add_text(
        self,
        content: str,
        source_type: str = "text",
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        try:
            payload = AddTextInput.model_validate(
                {
                    "content": content,
                    "source_type": source_type,
                    "title": title,
                    "idempotency_key": idempotency_key,
                }
            )
            item, job, deduplicated = await self.knowledge.add_text(
                payload.content,
                payload.source_type,
                payload.title,
                payload.idempotency_key,
            )
            return _bounded_result(
                {
                    "item_id": item.id,
                    "job_id": job.id,
                    "deduplicated": deduplicated,
                }
            )
        except Exception as error:
            raise _map_error(error) from error

    async def _add_url(
        self,
        url: str,
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        try:
            payload = AddUrlInput.model_validate(
                {"url": url, "title": title, "idempotency_key": idempotency_key}
            )
            item, job, deduplicated = await self.knowledge.add_url(
                payload.url,
                payload.title,
                payload.idempotency_key,
            )
            return _bounded_result(
                {
                    "item_id": item.id,
                    "job_id": job.id,
                    "deduplicated": deduplicated,
                }
            )
        except Exception as error:
            raise _map_error(error) from error

    async def _search_knowledge(
        self,
        query: str,
        limit: int = 6,
        source_types: list[str] | None = None,
    ) -> dict[str, object]:
        try:
            payload = SearchKnowledgeInput.model_validate(
                {"query": query, "limit": limit, "source_types": source_types}
            )
            result = await self.knowledge.search(
                payload.query,
                limit=payload.limit,
                source_types=payload.source_types,
            )
            return _bounded_result(result.model_dump(mode="json", exclude_none=True))
        except Exception as error:
            raise _map_error(error) from error

    async def _get_item(self, item_id: str) -> dict[str, object]:
        try:
            payload = GetItemInput.model_validate({"item_id": item_id})
            result = await self.knowledge.get_item(payload.item_id)
            if result.get("status") != "published" or result.get("pending_content_version_id"):
                raise ApplicationError(409, "item_not_published", "条目尚未发布")
            return _bounded_result(result)
        except Exception as error:
            raise _map_error(error) from error

    async def _list_collections(self, limit: int = 100) -> dict[str, object]:
        try:
            payload = ListCollectionsInput.model_validate({"limit": limit})
            return _bounded_result(
                {"collections": await self.knowledge.list_collections(limit=payload.limit)}
            )
        except Exception as error:
            raise _map_error(error) from error

    async def run_stdio(self) -> None:
        await self.server.run_stdio_async()


async def _run_stdio_application() -> None:
    from app.main import create_app

    application = create_app(start_background=False, serve_frontend=False)
    async with application.router.lifespan_context(application):
        provider = MCPKnowledgeServer(application.state.knowledge_service)
        await provider.run_stdio()


def main() -> None:
    asyncio.run(_run_stdio_application())


if __name__ == "__main__":
    main()
