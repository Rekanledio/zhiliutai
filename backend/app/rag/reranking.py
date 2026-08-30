from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable, Protocol

from app.rag.types import RetrievedChunk


class RerankerError(RuntimeError):
    """A local reranker failed; retrieval can continue with the RRF order."""


class RerankerProvider(Protocol):
    name: str
    model: str

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk]
    ) -> Mapping[str, float]: ...


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(unicodedata.normalize("NFKC", value).casefold()))


class KeywordOverlapReranker:
    """Deterministic local reference reranker for tests and offline evaluation."""

    name = "local-keyword-overlap"
    model = "keyword-overlap-v1"

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk]
    ) -> Mapping[str, float]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return {chunk.chunk_id: 0.0 for chunk in chunks}
        return {
            chunk.chunk_id: len(query_tokens & _tokens(chunk.content))
            / len(query_tokens)
            for chunk in chunks
        }


CrossEncoderLoader = Callable[..., Any]


def _default_cross_encoder_loader(model: str, **kwargs: Any) -> Any:
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as error:  # pragma: no cover - exercised through injected loaders
        raise RerankerError("本地 Reranker 依赖不可用") from error
    return CrossEncoder(model, **kwargs)


class SentenceTransformersReranker:
    """Lazy local cross-encoder adapter; callers retain RRF order on failure."""

    name = "sentence-transformers"

    def __init__(
        self,
        model: str,
        *,
        device: str = "cpu",
        cache_path: Path,
        loader: CrossEncoderLoader = _default_cross_encoder_loader,
        max_document_chars: int = 4_000,
        local_files_only: bool = True,
    ) -> None:
        self.model = model
        self.device = device
        self.cache_path = cache_path
        self.max_document_chars = max(1, max_document_chars)
        self.local_files_only = local_files_only
        self._loader = loader
        self._loaded: Any | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> Any:
        if self._loaded is None:
            self.cache_path.mkdir(parents=True, exist_ok=True)
            self._loaded = self._loader(
                self.model,
                device=self.device,
                cache_folder=str(self.cache_path),
                local_files_only=self.local_files_only,
            )
        return self._loaded

    def _predict(
        self,
        query: str,
        chunks: Sequence[RetrievedChunk],
    ) -> Mapping[str, float]:
        loaded = self._load()
        pairs = [
            (query, chunk.content[: self.max_document_chars])
            for chunk in chunks
        ]
        raw_scores = loaded.predict(pairs, show_progress_bar=False)
        scores = list(raw_scores)
        if len(scores) != len(chunks):
            raise RerankerError("本地 Reranker 返回数量无效")
        try:
            return {
                chunk.chunk_id: float(score)
                for chunk, score in zip(chunks, scores, strict=True)
            }
        except (TypeError, ValueError, OverflowError) as error:
            raise RerankerError("本地 Reranker 返回分数无效") from error

    async def rerank(
        self,
        query: str,
        chunks: Sequence[RetrievedChunk],
    ) -> Mapping[str, float]:
        if not chunks:
            return {}
        async with self._lock:
            try:
                return await asyncio.to_thread(self._predict, query, chunks)
            except RerankerError:
                raise
            except Exception as error:
                raise RerankerError("本地 Reranker 执行失败") from error
