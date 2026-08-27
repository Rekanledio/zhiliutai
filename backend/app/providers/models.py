import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import Settings


class ProviderNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class DraftResult:
    title: str
    body: str
    summary: str
    suggested_tags: list[str]
    prompt_version: str


class DraftProvider(Protocol):
    async def create_draft(self, title: str, content: str) -> DraftResult: ...


class EmbeddingProvider(Protocol):
    model: str
    version: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleDraftProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.chat_base_url or not settings.chat_model:
            raise ProviderNotConfigured("Chat capability is not configured")
        self.base_url = settings.chat_base_url.rstrip("/")
        self.model = settings.chat_model
        self.api_key = settings.chat_api_key

    async def create_draft(self, title: str, content: str) -> DraftResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        prompt = (
            "请整理以下个人知识输入。保留事实，不扩写未知信息。"
            "返回 JSON：title, body, summary, suggested_tags（字符串数组）。\n\n" + content
        )
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                },
            )
            response.raise_for_status()
            payload = response.json()
        parsed = json.loads(payload["choices"][0]["message"]["content"])
        return DraftResult(
            title=str(parsed.get("title") or title),
            body=str(parsed.get("body") or content),
            summary=str(parsed.get("summary") or ""),
            suggested_tags=[str(tag) for tag in parsed.get("suggested_tags", [])],
            prompt_version="stage2-draft-v1",
        )


class PassthroughDraftProvider:
    """Keeps ingestion usable when Chat is not configured; it never claims AI output."""

    async def create_draft(self, title: str, content: str) -> DraftResult:
        return DraftResult(
            title=title,
            body=content,
            summary="模型未配置；当前草稿保留规范化原文。",
            suggested_tags=[],
            prompt_version="passthrough-v1",
        )


class FastEmbedEmbeddingProvider:
    version = "fastembed-v1"

    def __init__(
        self,
        settings: Settings,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        if settings.embedding_provider != "fastembed" or not settings.embedding_model:
            raise ProviderNotConfigured("FastEmbed capability is not configured")
        if model_factory is None:
            from fastembed import TextEmbedding

            model_factory = TextEmbedding
        self.model = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self.cache_path = settings.embedding_cache_path
        self._model_factory = model_factory
        self._loaded_model: Any | None = None
        self._load_lock = asyncio.Lock()

    async def _get_model(self) -> Any:
        if self._loaded_model is not None:
            return self._loaded_model
        async with self._load_lock:
            if self._loaded_model is None:
                self.cache_path.mkdir(parents=True, exist_ok=True)
                self._loaded_model = await asyncio.to_thread(
                    self._model_factory,
                    model_name=self.model,
                    cache_dir=str(self.cache_path),
                )
        return self._loaded_model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = await self._get_model()

        def generate() -> list[list[float]]:
            vectors: list[list[float]] = []
            for vector in model.embed(texts):
                values = vector.tolist() if hasattr(vector, "tolist") else list(vector)
                vectors.append([float(value) for value in values])
            return vectors

        vectors = await asyncio.to_thread(generate)
        if len(vectors) != len(texts):
            raise ValueError("Embedding 返回数量与输入不一致")
        if any(len(vector) != self.dimensions for vector in vectors):
            raise ValueError("Embedding 维度与配置不一致")
        return vectors


class OpenAICompatibleEmbeddingProvider:
    version = "openai-compatible-v1"

    def __init__(self, settings: Settings) -> None:
        if not settings.embedding_base_url or not settings.embedding_model:
            raise ProviderNotConfigured("Embedding capability is not configured")
        self.base_url = settings.embedding_base_url.rstrip("/")
        self.model = settings.embedding_model
        self.api_key = settings.embedding_api_key
        self.dimensions = settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()
        vectors = [entry["embedding"] for entry in payload["data"]]
        if any(len(vector) != self.dimensions for vector in vectors):
            raise ValueError("Embedding 维度与配置不一致")
        return vectors
