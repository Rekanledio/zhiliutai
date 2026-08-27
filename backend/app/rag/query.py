from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class ProcessedQuery:
    original: str
    normalized: str
    fts_query: str | None
    tokens: tuple[str, ...]


class QueryProcessor:
    """Normalize user text and build a parameter-safe FTS5 expression."""

    def __init__(self, max_chars: int = 2_000) -> None:
        self.max_chars = max_chars

    def normalize(self, query: str) -> str:
        normalized = unicodedata.normalize("NFKC", query)
        normalized = " ".join(normalized.split())
        if not normalized:
            raise ValueError("查询不能为空")
        if len(normalized) > self.max_chars:
            raise ValueError(f"查询不能超过 {self.max_chars} 个字符")
        return normalized

    @staticmethod
    def tokenize(normalized_query: str) -> tuple[str, ...]:
        tokens = tuple(dict.fromkeys(_TOKEN_RE.findall(normalized_query)))
        return tokens

    @staticmethod
    def quote_token(token: str) -> str:
        return '"' + token.replace('"', '""') + '"'

    def build_fts_query(self, normalized_query: str) -> str | None:
        tokens = self.tokenize(normalized_query)
        if not tokens:
            return None
        return " OR ".join(self.quote_token(token) for token in tokens)

    def process(self, query: str) -> ProcessedQuery:
        normalized = self.normalize(query)
        tokens = self.tokenize(normalized)
        return ProcessedQuery(
            original=query,
            normalized=normalized,
            fts_query=self.build_fts_query(normalized),
            tokens=tokens,
        )
