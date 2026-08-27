import json
from uuid import uuid4

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, ContentVersion, KnowledgeItem
from app.providers.models import EmbeddingProvider
from app.services.content import chunk_content, content_hash
from app.services.vector_store import QdrantLocalStore, VectorRecord


def _locator_value(locator: object, fallback: str) -> str:
    if isinstance(locator, str):
        return locator
    if isinstance(locator, dict):
        return json.dumps(locator, ensure_ascii=False, sort_keys=True)
    return fallback


def _version_parts(version: ContentVersion, fallback_locator: str) -> list[tuple[str, str]]:
    try:
        metadata = json.loads(version.source_metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    segments = metadata.get("segments") if isinstance(metadata, dict) else None
    if not isinstance(segments, list):
        return [(part, fallback_locator) for part in chunk_content(version.body)]

    source_parts: list[tuple[str, str]] = []
    raw_texts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            return [(part, fallback_locator) for part in chunk_content(version.body)]
        text = segment["text"]
        raw_texts.append(text)
        source_parts.extend(
            (part, _locator_value(segment.get("locator"), fallback_locator))
            for part in chunk_content(text)
        )
    if not raw_texts or content_hash("\n\n".join(raw_texts)) != content_hash(version.body):
        return [(part, fallback_locator) for part in chunk_content(version.body)]
    return source_parts


class IndexService:
    def __init__(
        self,
        vector_store: QdrantLocalStore,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider

    async def index_version(
        self,
        session: AsyncSession,
        item: KnowledgeItem,
        version: ContentVersion,
        source_locator: str,
    ) -> list[Chunk]:
        parts = _version_parts(version, source_locator)
        vectors = await self.embedding_provider.embed([part for part, _ in parts])
        if len(parts) != len(vectors):
            raise ValueError("Embedding 返回数量不一致")
        chunks: list[Chunk] = []
        records: list[VectorRecord] = []
        for ordinal, ((part, locator), vector) in enumerate(zip(parts, vectors, strict=True)):
            chunk_id = str(uuid4())
            point_id = str(uuid4())
            chunk = Chunk(
                id=chunk_id,
                knowledge_item_id=item.id,
                content_version_id=version.id,
                ordinal=ordinal,
                content=part,
                content_hash=content_hash(part),
                source_type=item.source_type,
                source_locator=locator,
                embedding_model=self.embedding_provider.model,
                embedding_version=self.embedding_provider.version,
                qdrant_point_id=point_id,
            )
            chunks.append(chunk)
            records.append(
                VectorRecord(
                    point_id=point_id,
                    vector=vector,
                    chunk_id=chunk_id,
                    knowledge_item_id=item.id,
                    content_version_id=version.id,
                    source_type=item.source_type,
                    source_locator=locator,
                    embedding_model=self.embedding_provider.model,
                    embedding_version=self.embedding_provider.version,
                )
            )
        self.vector_store.upsert(records)
        await session.execute(delete(Chunk).where(Chunk.knowledge_item_id == item.id))
        await session.execute(
            text("DELETE FROM chunk_fts WHERE knowledge_item_id = :item_id"),
            {"item_id": item.id},
        )
        session.add_all(chunks)
        await session.flush()
        for chunk in chunks:
            await session.execute(
                text(
                    "INSERT INTO chunk_fts "
                    "(chunk_id, knowledge_item_id, content_version_id, content, source_locator) "
                    "VALUES (:chunk_id, :item_id, :version_id, :content, :locator)"
                ),
                {
                    "chunk_id": chunk.id,
                    "item_id": item.id,
                    "version_id": version.id,
                    "content": chunk.content,
                    "locator": chunk.source_locator,
                },
            )
        return chunks
