"""Shared knowledge application operations used by HTTP and MCP boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.core.paths import safe_relative_path
from app.core.safety import redact_sensitive_text
from app.db.models import Collection, CollectionItem
from app.rag.citations import CitationBuilder
from app.rag.retrieval import HybridRetriever
from app.schemas.rag import (
    CitationResponse,
    EvidenceResponse,
    RetrievalDiagnosticsResponse,
    SearchResponse,
    SearchResult,
)
from app.services.stage2 import Stage2Service

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class KnowledgeApplicationService:
    """Keep shared MCP/API operations on existing application service seams."""

    def __init__(
        self,
        stage2: Stage2Service,
        retriever: HybridRetriever,
        citation_builder: CitationBuilder,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.stage2 = stage2
        self.retriever = retriever
        self.citation_builder = citation_builder
        self.session_factory = session_factory

    async def add_text(
        self,
        content: str,
        source_type: str,
        title: str | None,
        idempotency_key: str | None,
    ):
        return await self.stage2.submit_text(content, source_type, title, idempotency_key)

    async def add_url(
        self,
        url: str,
        title: str | None,
        idempotency_key: str | None,
    ):
        return await self.stage2.submit_url(url, title, idempotency_key)

    async def search(
        self,
        query: str,
        *,
        limit: int = 6,
        source_types: Sequence[str] | None = None,
    ) -> SearchResponse:
        chunks, diagnostics, assessment = await self.retriever.retrieve(
            query,
            limit=limit,
            source_types=source_types,
        )
        citations = await self.citation_builder.build(chunks)
        results = [
            SearchResult(
                chunk_id=chunk.chunk_id,
                knowledge_item_id=chunk.knowledge_item_id,
                content_version_id=chunk.content_version_id,
                item_title=redact_sensitive_text(chunk.item_title),
                version_no=chunk.version_no,
                source_type=chunk.source_type,
                excerpt=citation.excerpt,
                citation=CitationResponse(**citation.as_dict()),
            )
            for chunk, citation in zip(chunks, citations, strict=True)
        ]
        return SearchResponse(
            query=redact_sensitive_text(query),
            normalized_query=redact_sensitive_text(diagnostics.normalized_query),
            results=results,
            evidence=EvidenceResponse(**assessment.as_dict()),
            diagnostics=RetrievalDiagnosticsResponse(**diagnostics.as_dict()),
            searched_at=datetime.now(timezone.utc),
        )

    async def get_item(self, item_id: str) -> dict[str, object]:
        item, version, binding = await self.stage2.get_item(item_id)
        relative_path = safe_relative_path(binding.relative_path) if binding else None
        if binding is not None and relative_path is None:
            raise ValueError("item path is invalid")
        body = version.body if version is not None else None
        result: dict[str, object] = {
            "id": item.id,
            "title": redact_sensitive_text(
                version.title if version is not None else item.title
            ),
            "source_type": redact_sensitive_text(item.source_type),
            "status": item.status,
            "content_hash": version.content_hash if version is not None else item.content_hash,
            "current_content_version_id": item.current_content_version_id,
            "pending_content_version_id": item.pending_content_version_id,
            "body": redact_sensitive_text(body) if body is not None else None,
            "note_relative_path": relative_path,
            "version_no": version.version_no if version is not None else None,
        }
        return result

    async def list_collections(self, *, limit: int = 100) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("合集数量无效")
        statement = (
            select(Collection, func.count(CollectionItem.id).label("item_count"))
            .outerjoin(CollectionItem, CollectionItem.collection_id == Collection.id)
            .group_by(Collection.id)
            .order_by(Collection.created_at.asc(), Collection.id.asc())
            .limit(limit)
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
        collections: list[dict[str, object]] = []
        for collection, item_count in rows:
            relative_description = (
                redact_sensitive_text(collection.description)
                if collection.description
                else None
            )
            collections.append(
                {
                    "id": collection.id,
                    "name": redact_sensitive_text(collection.name),
                    "description": relative_description,
                    "item_count": int(item_count),
                }
            )
        return collections
