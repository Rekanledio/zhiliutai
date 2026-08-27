from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceBlock:
    text: str
    locator: dict[str, Any]


@dataclass(frozen=True)
class ParsedSource:
    source_type: str
    media_type: str
    title: str
    body: str
    blocks: tuple[SourceBlock, ...]
    metadata: dict[str, Any]
