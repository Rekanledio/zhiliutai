from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Protocol

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
