from uuid import uuid4

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, ContentVersion, KnowledgeItem
from app.providers.models import EmbeddingProvider
from app.services.content import chunk_content, content_hash
from app.services.vector_store import QdrantLocalStore, VectorRecord


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
        parts = chunk_content(version.body)
        vectors = await self.embedding_provider.embed(parts)
        if len(parts) != len(vectors):
            raise ValueError("Embedding 返回数量不一致")
        chunks: list[Chunk] = []
        records: list[VectorRecord] = []
        for ordinal, (part, vector) in enumerate(zip(parts, vectors, strict=True)):
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
                source_locator=source_locator,
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
                    source_locator=source_locator,
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
