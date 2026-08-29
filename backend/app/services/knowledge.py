"""Shared knowledge application operations used by HTTP and MCP boundaries."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import ApplicationError
from app.core.paths import safe_relative_path
from app.core.safety import redact_sensitive_text
from app.db.models import Collection, CollectionItem, ContentVersion, KnowledgeItem
from app.obsidian.markdown import ObsidianVault, StagedWrite
from app.rag.citations import CitationBuilder
from app.rag.retrieval import HybridRetriever
from app.schemas.collections import (
    MAX_COLLECTION_DESCRIPTION_CHARS,
    MAX_COLLECTION_TEXT_CHARS,
    normalize_collection_names,
    normalize_collection_text,
)
from app.schemas.rag import (
    CitationResponse,
    EvidenceResponse,
    RetrievalDiagnosticsResponse,
    SearchResponse,
    SearchResult,
)
from app.services.stage2 import Stage2Service
from app.workflows.contracts import canonical_uuid

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


logger = structlog.get_logger("knowledge")
_UNSET = object()


@dataclass
class _StagedCollectionWrite:
    vault: ObsidianVault
    staged: StagedWrite
    old_raw: bytes
    relative_path: str
    swap_attempted: bool = False


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

    @staticmethod
    def _collection_summary(
        collection: Collection, item_count: int
    ) -> dict[str, object]:
        return {
            "id": collection.id,
            "name": redact_sensitive_text(collection.name),
            "description": (
                redact_sensitive_text(collection.description)
                if collection.description
                else None
            ),
            "item_count": int(item_count),
        }

    @staticmethod
    def _safe_tags(version: ContentVersion) -> list[str]:
        try:
            parsed = json.loads(version.suggested_tags_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in parsed:
            if not isinstance(value, str):
                continue
            tag = value.strip()
            if (
                not tag
                or len(tag) > 80
                or any(ord(character) < 32 or ord(character) == 127 for character in tag)
                or redact_sensitive_text(tag) != tag
            ):
                continue
            folded = tag.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            result.append(tag)
            if len(result) >= 50:
                break
        return result

    @classmethod
    def _item_projection(
        cls, item: KnowledgeItem, version: ContentVersion
    ) -> dict[str, object]:
        return {
            "id": item.id,
            "title": redact_sensitive_text(version.title),
            "source_type": redact_sensitive_text(item.source_type),
            "version_no": version.version_no,
            "suggested_tags": cls._safe_tags(version),
        }

    @staticmethod
    def _canonical_id(value: object, *, resource: str) -> str:
        try:
            return canonical_uuid(value)
        except ValueError as error:
            code = "collection_not_found" if resource == "collection" else "item_not_found"
            message = "合集不存在" if resource == "collection" else "知识条目不存在"
            raise ApplicationError(404, code, message) from error

    @staticmethod
    def _normalize_name(value: object) -> str:
        try:
            normalized = normalize_collection_text(
                value,
                max_length=MAX_COLLECTION_TEXT_CHARS,
            )
        except ValueError as error:
            raise ApplicationError(422, "invalid_collection_input", "合集内容无效") from error
        assert normalized is not None
        return normalized

    @staticmethod
    def _normalize_description(value: object) -> str | None:
        try:
            return normalize_collection_text(
                value,
                max_length=MAX_COLLECTION_DESCRIPTION_CHARS,
                allow_empty=True,
            )
        except ValueError as error:
            raise ApplicationError(422, "invalid_collection_input", "合集内容无效") from error

    @staticmethod
    async def _check_name_conflict(
        session: AsyncSession,
        name: str,
        *,
        exclude_id: str | None = None,
    ) -> None:
        statement = select(Collection.id).where(func.lower(Collection.name) == name.casefold())
        if exclude_id is not None:
            statement = statement.where(Collection.id != exclude_id)
        if (await session.execute(statement)).scalar_one_or_none() is not None:
            raise ApplicationError(409, "collection_name_conflict", "合集名称已存在")

    @staticmethod
    def _valid_members_statement(collection_id: str):
        return (
            select(CollectionItem, KnowledgeItem, ContentVersion)
            .join(KnowledgeItem, KnowledgeItem.id == CollectionItem.knowledge_item_id)
            .join(
                ContentVersion,
                and_(
                    ContentVersion.id == KnowledgeItem.current_content_version_id,
                    ContentVersion.knowledge_item_id == KnowledgeItem.id,
                ),
            )
            .where(
                CollectionItem.collection_id == collection_id,
                KnowledgeItem.deleted_at.is_(None),
                KnowledgeItem.status == "published",
                KnowledgeItem.current_content_version_id.is_not(None),
            )
            .order_by(KnowledgeItem.title.asc(), KnowledgeItem.id.asc())
        )

    async def _valid_members(
        self, session: AsyncSession, collection_id: str
    ) -> list[tuple[KnowledgeItem, ContentVersion]]:
        rows = (await session.execute(self._valid_members_statement(collection_id))).all()
        return [(item, version) for _relation, item, version in rows]

    async def _current_item(
        self, session: AsyncSession, item_id: str
    ) -> tuple[KnowledgeItem, ContentVersion]:
        canonical_item_id = self._canonical_id(item_id, resource="item")
        item = await session.get(KnowledgeItem, canonical_item_id)
        if item is None or item.deleted_at is not None:
            raise ApplicationError(404, "item_not_found", "知识条目不存在")
        if item.status != "published" or not item.current_content_version_id:
            raise ApplicationError(409, "collection_item_invalid", "只有已发布条目可加入合集")
        version = await session.get(ContentVersion, item.current_content_version_id)
        if version is None or version.knowledge_item_id != item.id:
            raise ApplicationError(409, "collection_item_invalid", "条目的当前版本无效")
        return item, version

    @staticmethod
    async def _is_current_item(
        session: AsyncSession, item: KnowledgeItem | None
    ) -> bool:
        if (
            item is None
            or item.deleted_at is not None
            or item.status != "published"
            or not item.current_content_version_id
        ):
            return False
        version = await session.get(ContentVersion, item.current_content_version_id)
        return version is not None and version.knowledge_item_id == item.id

    async def _relation_names(self, session: AsyncSession, item_id: str) -> list[str]:
        result = await session.execute(
            select(Collection.name)
            .join(CollectionItem, CollectionItem.collection_id == Collection.id)
            .where(CollectionItem.knowledge_item_id == item_id)
            .order_by(func.lower(Collection.name), Collection.name)
        )
        try:
            return normalize_collection_names(
                [name for name in result.scalars() if isinstance(name, str)]
            )
        except ValueError as error:
            raise ApplicationError(
                409,
                "collection_invalid_state",
                "合集关系包含不允许的内容",
            ) from error

    async def _stage_members(
        self,
        session: AsyncSession,
        members: list[tuple[KnowledgeItem, ContentVersion]],
        *,
        replacement: tuple[str, str] | None = None,
        remove_name: str | None = None,
    ) -> list[_StagedCollectionWrite]:
        writes: list[_StagedCollectionWrite] = []
        for item, _version in members:
            names = await self._relation_names(session, item.id)
            if replacement is not None:
                old_name, new_name = replacement
                names = [
                    new_name if name.casefold() == old_name.casefold() else name
                    for name in names
                ]
            if remove_name is not None:
                names = [
                    name for name in names if name.casefold() != remove_name.casefold()
                ]
            names = sorted(
                normalize_collection_names(names),
                key=lambda value: (value.casefold(), value),
            )
            vault, staged, old_raw, relative_path = await self.stage2.stage_collection_note(
                session, item, names
            )
            writes.append(
                _StagedCollectionWrite(
                    vault=vault,
                    staged=staged,
                    old_raw=old_raw,
                    relative_path=relative_path,
                )
            )
        return writes

    @staticmethod
    def _commit_staged(writes: list[_StagedCollectionWrite]) -> None:
        for write in writes:
            write.swap_attempted = True
            write.vault.commit_staged(write.staged)

    @staticmethod
    def _compensate_staged(writes: list[_StagedCollectionWrite]) -> None:
        for write in reversed(writes):
            try:
                if write.swap_attempted:
                    restore = write.vault.stage_bytes(write.relative_path, write.old_raw)
                    try:
                        write.vault.commit_staged(restore)
                    finally:
                        write.vault.discard_staged(restore)
                else:
                    write.vault.discard_staged(write.staged)
            except (OSError, ValueError) as error:
                logger.error(
                    "collection_vault_compensation_failed",
                    error_type=type(error).__name__,
                )

    async def list_collections(self, *, limit: int = 100) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("合集数量无效")
        valid_counts = (
            select(
                CollectionItem.collection_id.label("collection_id"),
                func.count(CollectionItem.id).label("item_count"),
            )
            .join(KnowledgeItem, KnowledgeItem.id == CollectionItem.knowledge_item_id)
            .join(
                ContentVersion,
                and_(
                    ContentVersion.id == KnowledgeItem.current_content_version_id,
                    ContentVersion.knowledge_item_id == KnowledgeItem.id,
                ),
            )
            .where(
                KnowledgeItem.deleted_at.is_(None),
                KnowledgeItem.status == "published",
                KnowledgeItem.current_content_version_id.is_not(None),
            )
            .group_by(CollectionItem.collection_id)
            .subquery()
        )
        statement = (
            select(Collection, func.coalesce(valid_counts.c.item_count, 0).label("item_count"))
            .outerjoin(valid_counts, valid_counts.c.collection_id == Collection.id)
            .order_by(Collection.created_at.asc(), Collection.id.asc())
            .limit(limit)
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            self._collection_summary(collection, int(item_count))
            for collection, item_count in rows
        ]

    async def get_collection(self, collection_id: str) -> dict[str, object]:
        canonical_collection_id = self._canonical_id(collection_id, resource="collection")
        async with self.session_factory() as session:
            collection = await session.get(Collection, canonical_collection_id)
            if collection is None:
                raise ApplicationError(404, "collection_not_found", "合集不存在")
            members = await self._valid_members(session, collection.id)
        items = [self._item_projection(item, version) for item, version in members]
        related_tags: list[str] = []
        seen_tags: set[str] = set()
        for item in items:
            for tag in item["suggested_tags"]:
                if not isinstance(tag, str) or tag.casefold() in seen_tags:
                    continue
                seen_tags.add(tag.casefold())
                related_tags.append(tag)
                if len(related_tags) >= 50:
                    break
            if len(related_tags) >= 50:
                break
        return {
            **self._collection_summary(collection, len(items)),
            "items": items,
            "related_tags": related_tags,
            "moc_enabled": False,
            "moc_status": "not_enabled",
        }

    async def create_collection(
        self, name: str, description: str | None = None
    ) -> dict[str, object]:
        normalized_name = self._normalize_name(name)
        normalized_description = self._normalize_description(description)
        collection_id: str
        async with self.stage2.mutation_lock:
            try:
                async with self.session_factory() as session, session.begin():
                    await self._check_name_conflict(session, normalized_name)
                    collection = Collection(
                        name=normalized_name,
                        description=normalized_description,
                    )
                    session.add(collection)
                    await session.flush()
                    collection_id = collection.id
            except IntegrityError as error:
                raise ApplicationError(
                    409, "collection_name_conflict", "合集名称已存在"
                ) from error
        return await self.get_collection(collection_id)

    async def update_collection(
        self,
        collection_id: str,
        *,
        name: str | object = _UNSET,
        description: str | None | object = _UNSET,
    ) -> dict[str, object]:
        canonical_collection_id = self._canonical_id(collection_id, resource="collection")
        normalized_name = self._normalize_name(name) if name is not _UNSET else None
        normalized_description = (
            self._normalize_description(description)
            if description is not _UNSET
            else None
        )
        writes: list[_StagedCollectionWrite] = []
        try:
            async with self.stage2.mutation_lock:
                async with self.session_factory() as session, session.begin():
                    collection = await session.get(Collection, canonical_collection_id)
                    if collection is None:
                        raise ApplicationError(404, "collection_not_found", "合集不存在")
                    rename = (
                        normalized_name is not None
                        and normalized_name != collection.name
                    )
                    if normalized_name is not None:
                        await self._check_name_conflict(
                            session,
                            normalized_name,
                            exclude_id=collection.id,
                        )
                    if rename:
                        members = await self._valid_members(session, collection.id)
                        writes = await self._stage_members(
                            session,
                            members,
                            replacement=(collection.name, normalized_name),
                        )
                        self._commit_staged(writes)
                        collection.name = normalized_name
                    if description is not _UNSET:
                        collection.description = normalized_description
                    await session.flush()
        except IntegrityError as error:
            self._compensate_staged(writes)
            raise ApplicationError(
                409, "collection_name_conflict", "合集名称已存在"
            ) from error
        except BaseException:
            self._compensate_staged(writes)
            raise
        return await self.get_collection(canonical_collection_id)

    async def delete_collection(self, collection_id: str) -> None:
        canonical_collection_id = self._canonical_id(collection_id, resource="collection")
        writes: list[_StagedCollectionWrite] = []
        try:
            async with self.stage2.mutation_lock:
                async with self.session_factory() as session, session.begin():
                    collection = await session.get(Collection, canonical_collection_id)
                    if collection is None:
                        raise ApplicationError(404, "collection_not_found", "合集不存在")
                    members = await self._valid_members(session, collection.id)
                    writes = await self._stage_members(
                        session,
                        members,
                        remove_name=collection.name,
                    )
                    self._commit_staged(writes)
                    await session.execute(
                        delete(CollectionItem).where(
                            CollectionItem.collection_id == collection.id
                        )
                    )
                    await session.delete(collection)
        except BaseException:
            self._compensate_staged(writes)
            raise

    async def add_collection_item(
        self, collection_id: str, item_id: str
    ) -> dict[str, object]:
        canonical_collection_id = self._canonical_id(collection_id, resource="collection")
        canonical_item_id = self._canonical_id(item_id, resource="item")
        writes: list[_StagedCollectionWrite] = []
        try:
            async with self.stage2.mutation_lock:
                async with self.session_factory() as session, session.begin():
                    collection = await session.get(Collection, canonical_collection_id)
                    if collection is None:
                        raise ApplicationError(404, "collection_not_found", "合集不存在")
                    item, _version = await self._current_item(session, canonical_item_id)
                    relation_result = await session.execute(
                        select(CollectionItem).where(
                            CollectionItem.collection_id == collection.id,
                            CollectionItem.knowledge_item_id == item.id,
                        )
                    )
                    relation = relation_result.scalar_one_or_none()
                    names = await self._relation_names(session, item.id)
                    if collection.name.casefold() not in {name.casefold() for name in names}:
                        names.append(collection.name)
                    names = sorted(
                        normalize_collection_names(names),
                        key=lambda value: (value.casefold(), value),
                    )
                    vault, staged, old_raw, relative_path = (
                        await self.stage2.stage_collection_note(
                            session,
                            item,
                            names,
                            skip_if_unchanged=relation is not None,
                        )
                    )
                    if staged is not None:
                        writes = [
                            _StagedCollectionWrite(
                                vault=vault,
                                staged=staged,
                                old_raw=old_raw,
                                relative_path=relative_path,
                            )
                        ]
                        self._commit_staged(writes)
                    if relation is None:
                        session.add(
                            CollectionItem(
                                collection_id=collection.id,
                                knowledge_item_id=item.id,
                            )
                        )
        except IntegrityError as error:
            self._compensate_staged(writes)
            raise ApplicationError(
                409, "collection_item_conflict", "合集关系无法更新"
            ) from error
        except BaseException:
            self._compensate_staged(writes)
            raise
        return await self.get_collection(canonical_collection_id)

    async def remove_collection_item(
        self, collection_id: str, item_id: str
    ) -> dict[str, object]:
        canonical_collection_id = self._canonical_id(collection_id, resource="collection")
        canonical_item_id = self._canonical_id(item_id, resource="item")
        writes: list[_StagedCollectionWrite] = []
        try:
            async with self.stage2.mutation_lock:
                async with self.session_factory() as session, session.begin():
                    collection = await session.get(Collection, canonical_collection_id)
                    if collection is None:
                        raise ApplicationError(404, "collection_not_found", "合集不存在")
                    relation_result = await session.execute(
                        select(CollectionItem).where(
                            CollectionItem.collection_id == collection.id,
                            CollectionItem.knowledge_item_id == canonical_item_id,
                        )
                    )
                    relation = relation_result.scalar_one_or_none()
                    if relation is not None:
                        item = await session.get(KnowledgeItem, canonical_item_id)
                        if await self._is_current_item(session, item):
                            assert item is not None
                            names = [
                                name
                                for name in await self._relation_names(session, item.id)
                                if name.casefold() != collection.name.casefold()
                            ]
                            names = sorted(
                                normalize_collection_names(names),
                                key=lambda value: (value.casefold(), value),
                            )
                            vault, staged, old_raw, relative_path = await self.stage2.stage_collection_note(
                                session, item, names
                            )
                            writes = [
                                _StagedCollectionWrite(
                                    vault=vault,
                                    staged=staged,
                                    old_raw=old_raw,
                                    relative_path=relative_path,
                                )
                            ]
                            self._commit_staged(writes)
                        await session.delete(relation)
        except BaseException:
            self._compensate_staged(writes)
            raise
        return await self.get_collection(canonical_collection_id)
