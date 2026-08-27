import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ApplicationError
from app.db.models import (
    ContentVersion,
    KnowledgeItem,
    NoteBinding,
    ProcessingJob,
    SourceArtifact,
)
from app.ingestion.fetcher import SourceFetcher, UnsafeUrlError
from app.ingestion.parsers import parse_source
from app.ingestion.types import ParsedSource, SourceBlock
from app.obsidian.markdown import ObsidianVault, parse_note, render_note
from app.providers.models import DraftProvider, EmbeddingProvider
from app.services.artifacts import ArtifactStore, StoredArtifact
from app.services.content import content_hash, default_title, normalize_content
from app.services.indexing import IndexService
from app.services.vector_store import QdrantLocalStore


class Stage2Service:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        draft_provider: DraftProvider,
        embedding_provider: EmbeddingProvider | None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.artifacts = ArtifactStore(settings.artifact_root)
        self.draft_provider = draft_provider
        self._mutation_lock = asyncio.Lock()
        self._initial_vector_reconciliation_complete = False
        self.embedding_provider = embedding_provider
        self.vector_store = QdrantLocalStore(settings.qdrant_path, settings.embedding_dimensions)
        self.source_fetcher = SourceFetcher(
            max_bytes=settings.source_max_bytes,
            timeout=settings.source_fetch_timeout,
            max_redirects=settings.source_max_redirects,
        )

    def vault(self) -> ObsidianVault:
        if self.settings.vault_root is None:
            raise ApplicationError(409, "vault_not_configured", "尚未配置 Obsidian Vault")
        return ObsidianVault(self.settings.vault_root, self.settings.managed_vault_dir)

    async def submit_file(
        self,
        content: bytes,
        filename: str | None,
        media_type: str | None,
        title: str | None,
        idempotency_key: str | None,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        if len(content) > self.settings.source_max_bytes:
            raise ApplicationError(413, "source_too_large", "来源文件超过大小限制")
        suffix = Path(filename or "").suffix.lower()
        source_by_suffix = {
            ".pdf": ("pdf", "application/pdf"),
            ".docx": (
                "docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        }
        source_type, default_media_type = source_by_suffix.get(suffix, (None, None))
        if source_type is None:
            raise ApplicationError(422, "unsupported_file_type", "仅支持 PDF 和 DOCX 文件")
        stored = self.artifacts.put_bytes(content, suffix)
        source_locator = json.dumps(
            {
                "kind": "file_upload",
                "filename": Path(filename or f"source{suffix}").name,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return await self._submit_artifact(
            stored,
            source_type=source_type,
            media_type=media_type or default_media_type,
            title=title,
            fallback_title=Path(filename or "").stem or f"{source_type} 来源",
            idempotency_key=idempotency_key,
            source_locator=source_locator,
        )

    async def submit_url(
        self,
        url: str,
        title: str | None,
        idempotency_key: str | None,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        try:
            self.source_fetcher.validate(url)
        except UnsafeUrlError as error:
            raise ApplicationError(422, "unsafe_url", str(error)) from error
        stored = self.artifacts.put_text(url, ".url")
        source_locator = json.dumps(
            {"kind": "url_request", "url": url},
            ensure_ascii=False,
            sort_keys=True,
        )
        return await self._submit_artifact(
            stored,
            source_type="webpage",
            media_type="text/uri-list",
            title=title,
            fallback_title=urlsplit(url).hostname or "网页来源",
            idempotency_key=idempotency_key,
            source_locator=source_locator,
            payload_extra={"url": url},
        )

    async def _submit_artifact(
        self,
        stored: StoredArtifact,
        *,
        source_type: str,
        media_type: str,
        title: str | None,
        fallback_title: str,
        idempotency_key: str | None,
        source_locator: str,
        payload_extra: dict[str, object] | None = None,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        async with self.session_factory() as session, session.begin():
            if idempotency_key:
                existing_job_result = await session.execute(
                    select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
                )
                existing_job = existing_job_result.scalar_one_or_none()
                if existing_job is not None:
                    existing_payload = json.loads(existing_job.payload_json)
                    existing_item = await session.get(
                        KnowledgeItem, existing_payload.get("item_id")
                    )
                    if existing_item is None or existing_item.content_hash != stored.content_hash:
                        raise ApplicationError(
                            409,
                            "idempotency_conflict",
                            "同一幂等键已用于不同内容",
                        )
                    return existing_item, existing_job, True
            duplicate_result = await session.execute(
                select(KnowledgeItem)
                .where(
                    KnowledgeItem.content_hash == stored.content_hash,
                    KnowledgeItem.deleted_at.is_(None),
                )
                .order_by(KnowledgeItem.created_at)
                .limit(1)
            )
            duplicate = duplicate_result.scalar_one_or_none()
            if duplicate is not None:
                job_result = await session.execute(
                    select(ProcessingJob)
                    .where(ProcessingJob.payload_json.like(f'%"item_id": "{duplicate.id}"%'))
                    .order_by(ProcessingJob.created_at.desc())
                    .limit(1)
                )
                existing_job = job_result.scalar_one_or_none()
                if existing_job is None:
                    existing_job = ProcessingJob(
                        kind="ingest_source",
                        state="succeeded",
                        stage="deduplicated",
                        progress=1.0,
                        payload_json=json.dumps(
                            {"item_id": duplicate.id}, ensure_ascii=False
                        ),
                        result_json=json.dumps(
                            {"item_id": duplicate.id}, ensure_ascii=False
                        ),
                    )
                    session.add(existing_job)
                return duplicate, existing_job, True

            item = KnowledgeItem(
                title=(title or fallback_title)[:300],
                source_type=source_type,
                status="processing",
                content_hash=stored.content_hash,
            )
            session.add(item)
            await session.flush()
            artifact = SourceArtifact(
                knowledge_item_id=item.id,
                artifact_type="original_input",
                media_type=media_type,
                relative_path=stored.relative_path,
                content_hash=stored.content_hash,
                byte_size=stored.byte_size,
                source_locator=source_locator,
            )
            session.add(artifact)
            await session.flush()
            payload: dict[str, object] = {
                "item_id": item.id,
                "artifact_id": artifact.id,
                "source_type": source_type,
                "title_provided": title is not None,
            }
            if payload_extra:
                payload.update(payload_extra)
            job = ProcessingJob(
                kind="ingest_source",
                payload_json=json.dumps(payload, ensure_ascii=False),
                idempotency_key=idempotency_key,
            )
            session.add(job)
            await session.flush()
            return item, job, False

    async def submit_text(
        self,
        content: str,
        source_type: str,
        title: str | None,
        idempotency_key: str | None,
    ) -> tuple[KnowledgeItem, ProcessingJob, bool]:
        normalized = normalize_content(content)
        digest = content_hash(normalized)
        stored = self.artifacts.put_text(normalized, ".md" if source_type == "markdown" else ".txt")
        async with self.session_factory() as session, session.begin():
            if idempotency_key:
                existing_job_result = await session.execute(
                    select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
                )
                existing_job = existing_job_result.scalar_one_or_none()
                if existing_job is not None:
                    existing_payload = json.loads(existing_job.payload_json)
                    existing_item = await session.get(
                        KnowledgeItem, existing_payload.get("item_id")
                    )
                    if existing_item is None or existing_item.content_hash != digest:
                        raise ApplicationError(
                            409,
                            "idempotency_conflict",
                            "同一幂等键已用于不同内容",
                        )
                    return existing_item, existing_job, True
            duplicate_result = await session.execute(
                select(KnowledgeItem)
                .where(
                    KnowledgeItem.content_hash == digest,
                    KnowledgeItem.deleted_at.is_(None),
                )
                .order_by(KnowledgeItem.created_at)
                .limit(1)
            )
            duplicate = duplicate_result.scalar_one_or_none()
            if duplicate is not None:
                job_result = await session.execute(
                    select(ProcessingJob)
                    .where(ProcessingJob.payload_json.like(f'%"item_id": "{duplicate.id}"%'))
                    .order_by(ProcessingJob.created_at.desc())
                    .limit(1)
                )
                existing_job = job_result.scalar_one_or_none()
                if existing_job is None:
                    existing_job = ProcessingJob(
                        kind="ingest_text",
                        state="succeeded",
                        stage="deduplicated",
                        progress=1.0,
                        payload_json=json.dumps({"item_id": duplicate.id}),
                        result_json=json.dumps({"item_id": duplicate.id}),
                    )
                    session.add(existing_job)
                return duplicate, existing_job, True

            item = KnowledgeItem(
                title=(title or default_title(normalized))[:300],
                source_type=source_type,
                status="processing",
                content_hash=digest,
            )
            session.add(item)
            await session.flush()
            artifact = SourceArtifact(
                knowledge_item_id=item.id,
                artifact_type="original_input",
                media_type="text/markdown" if source_type == "markdown" else "text/plain",
                relative_path=stored.relative_path,
                content_hash=stored.content_hash,
                byte_size=stored.byte_size,
                source_locator="inbox",
            )
            session.add(artifact)
            await session.flush()
            job = ProcessingJob(
                kind="ingest_text",
                payload_json=json.dumps(
                    {
                        "item_id": item.id,
                        "artifact_id": artifact.id,
                        "source_type": source_type,
                        "title_provided": title is not None,
                    },
                    ensure_ascii=False,
                ),
                idempotency_key=idempotency_key,
            )
            session.add(job)
            await session.flush()
            return item, job, False

    async def process_ingestion(self, job: ProcessingJob) -> dict[str, object]:
        payload = json.loads(job.payload_json)
        async with self.session_factory() as session:
            item = await session.get(KnowledgeItem, payload["item_id"])
            artifact = await session.get(SourceArtifact, payload["artifact_id"])
        if item is None or artifact is None:
            raise RuntimeError("采集记录不完整")

        source_type = str(payload.get("source_type") or item.source_type)
        snapshot: SourceArtifact | None = None
        if source_type == "webpage":
            url = payload.get("url")
            if not isinstance(url, str) or not url:
                raise RuntimeError("网页来源缺少 URL")
            try:
                fetched = await self.source_fetcher.fetch(url)
            except UnsafeUrlError as error:
                raise ApplicationError(422, "unsafe_url", str(error)) from error
            stored_snapshot = self.artifacts.put_bytes(fetched.content, ".html")
            async with self.session_factory() as session, session.begin():
                item = await session.get(KnowledgeItem, item.id)
                if item is None:
                    raise RuntimeError("知识条目不存在")
                snapshot = SourceArtifact(
                    knowledge_item_id=item.id,
                    artifact_type="web_snapshot",
                    media_type=fetched.media_type,
                    relative_path=stored_snapshot.relative_path,
                    content_hash=stored_snapshot.content_hash,
                    byte_size=stored_snapshot.byte_size,
                    source_locator=json.dumps(
                        {
                            "kind": "web_snapshot",
                            "requested_url": fetched.requested_url,
                            "url": fetched.final_url,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                session.add(snapshot)
                await session.flush()
            parsed = parse_source(
                source_type,
                fetched.content,
                url=fetched.final_url,
            )
        elif source_type in {"text", "markdown"}:
            content = self.artifacts.read_text(artifact.relative_path)
            body = normalize_content(content)
            block = SourceBlock(
                body.rstrip("\n"),
                {"kind": source_type, "source_locator": artifact.source_locator or "inbox"},
            )
            parsed = ParsedSource(
                source_type=source_type,
                media_type=artifact.media_type,
                title=default_title(body),
                body=body,
                blocks=(block,),
                metadata={
                    "source_type": source_type,
                    "media_type": artifact.media_type,
                    "title": default_title(body),
                    "segments": [{"text": block.text, "locator": block.locator}],
                },
            )
        else:
            parsed = parse_source(
                source_type,
                self.artifacts.read_bytes(artifact.relative_path),
            )

        title_provided = bool(payload.get("title_provided"))
        draft_title = item.title if title_provided else parsed.title
        draft = await self.draft_provider.create_draft(draft_title, parsed.body)
        async with self.session_factory() as session, session.begin():
            item = await session.get(KnowledgeItem, item.id)
            if item is None:
                raise RuntimeError("知识条目不存在")
            next_no = await self._next_version_no(session, item.id)
            version = ContentVersion(
                knowledge_item_id=item.id,
                version_no=next_no,
                source_kind="draft",
                title=draft.title[:300],
                body=normalize_content(draft.body),
                content_hash=content_hash(draft.body),
                summary=draft.summary,
                suggested_tags_json=json.dumps(draft.suggested_tags, ensure_ascii=False),
                prompt_version=draft.prompt_version,
                source_metadata_json=json.dumps(parsed.metadata, ensure_ascii=False),
            )
            session.add(version)
            await session.flush()
            if not title_provided:
                item.title = version.title
            item.status = "pending_review"
            item.current_content_version_id = version.id
            item.updated_at = datetime.now(timezone.utc)
            result: dict[str, object] = {
                "item_id": item.id,
                "content_version_id": version.id,
                "source_type": source_type,
            }
            if snapshot is not None:
                result["snapshot_artifact_id"] = snapshot.id
            return result

    async def _next_version_no(self, session: AsyncSession, item_id: str) -> int:
        result = await session.execute(
            select(func.max(ContentVersion.version_no)).where(
                ContentVersion.knowledge_item_id == item_id
            )
        )
        return int(result.scalar_one() or 0) + 1

    async def review(self, item_id: str) -> KnowledgeItem:
        async with self.session_factory() as session, session.begin():
            item = await self._item(session, item_id)
            if item.status != "pending_review":
                raise ApplicationError(409, "invalid_item_state", "条目不在待审核状态")
            item.status = "reviewed"
            item.updated_at = datetime.now(timezone.utc)
            await session.flush()
            return item

    async def patch_item(
        self,
        item_id: str,
        title: str | None,
        body: str | None,
        expected_content_hash: str | None,
    ) -> KnowledgeItem:
        async with self._mutation_lock:
            item = await self._patch_item(item_id, title, body, expected_content_hash)
            if item.status == "published" and item.current_content_version_id:
                self.vector_store.delete_item_except_version(
                    item.id, item.current_content_version_id
                )
            return item

    async def _patch_item(
        self,
        item_id: str,
        title: str | None,
        body: str | None,
        expected_content_hash: str | None,
    ) -> KnowledgeItem:
        async with self.session_factory() as session, session.begin():
            item = await self._item(session, item_id)
            if item.status == "published":
                return await self._patch_published(
                    session, item, title, body, expected_content_hash
                )
            if item.status not in {"pending_review", "reviewed"}:
                raise ApplicationError(409, "invalid_item_state", "当前状态不可编辑")
            current = await self._version(session, item)
            next_body = normalize_content(body if body is not None else current.body)
            version = ContentVersion(
                knowledge_item_id=item.id,
                version_no=await self._next_version_no(session, item.id),
                source_kind="draft",
                title=(title or current.title)[:300],
                body=next_body,
                content_hash=content_hash(next_body),
                summary=current.summary,
                suggested_tags_json=current.suggested_tags_json,
                prompt_version=current.prompt_version,
                source_metadata_json=current.source_metadata_json,
            )
            session.add(version)
            await session.flush()
            item.title = version.title
            item.content_hash = version.content_hash
            item.current_content_version_id = version.id
            item.status = "pending_review"
            item.updated_at = datetime.now(timezone.utc)
            return item

    async def _patch_published(
        self,
        session: AsyncSession,
        item: KnowledgeItem,
        title: str | None,
        body: str | None,
        expected_content_hash: str | None,
    ) -> KnowledgeItem:
        binding_result = await session.execute(
            select(NoteBinding).where(NoteBinding.knowledge_item_id == item.id)
        )
        binding = binding_result.scalar_one_or_none()
        if binding is None:
            raise ApplicationError(409, "note_binding_missing", "发布条目缺少笔记绑定")
        vault = self.vault()
        current = await self._version(session, item)
        disk_note = vault.read(binding.relative_path)
        disk_hash = content_hash(disk_note.body)
        if not expected_content_hash or expected_content_hash != disk_hash:
            raise ApplicationError(
                409,
                "content_conflict",
                "Obsidian 内容已变化，请重新载入后再编辑",
                {"current_content_hash": disk_hash},
            )
        next_title = (title or item.title)[:300]
        next_body = normalize_content(body if body is not None else disk_note.body)
        raw = render_note(
            zhiliu_id=binding.zhiliu_id,
            source_type=item.source_type,
            title=next_title,
            body=next_body,
            status="reviewed",
            created_at=item.created_at,
            updated_at=datetime.now(timezone.utc),
            tags=[str(tag) for tag in disk_note.metadata.get("tags", []) if isinstance(tag, str)],
            source_url=self._source_url(current.source_metadata_json),
        )
        vault.atomic_write(binding.relative_path, raw)
        source_metadata_json = self._vault_source_metadata(
            item,
            next_body,
            binding.relative_path,
            source_url=self._source_url(current.source_metadata_json),
        )
        await self._apply_vault_version(
            session,
            item,
            binding,
            next_title,
            next_body,
            binding.relative_path,
            source_metadata_json=source_metadata_json,
        )
        return item

    async def publish(self, item_id: str) -> KnowledgeItem:
        async with self._mutation_lock:
            item = await self._publish(item_id)
            if item.current_content_version_id:
                self.vector_store.delete_item_except_version(
                    item.id, item.current_content_version_id
                )
            return item

    async def _publish(self, item_id: str) -> KnowledgeItem:
        if self.embedding_provider is None:
            raise ApplicationError(
                409,
                "embedding_not_configured",
                "发布前必须配置 Embedding capability，以完成 Qdrant 索引",
            )
        vault = self.vault()
        async with self.session_factory() as session, session.begin():
            item = await self._item(session, item_id)
            if item.status == "published":
                return item
            if item.status != "reviewed":
                raise ApplicationError(409, "invalid_item_state", "条目必须先审核再发布")
            current = await self._version(session, item)
            binding_result = await session.execute(
                select(NoteBinding).where(NoteBinding.knowledge_item_id == item.id)
            )
            binding = binding_result.scalar_one_or_none()
            relative_path = (
                binding.relative_path
                if binding is not None
                else vault.publish_path(item.title, item.id)
            )
            raw = render_note(
                zhiliu_id=item.id,
                source_type=item.source_type,
                title=current.title,
                body=current.body,
                status="reviewed",
                created_at=item.created_at,
                updated_at=datetime.now(timezone.utc),
                tags=json.loads(current.suggested_tags_json),
                source_url=self._source_url(current.source_metadata_json),
            )
            vault.atomic_write(relative_path, raw)
            written = vault.read(relative_path)
            if content_hash(written.body) != content_hash(current.body):
                raise OSError("Vault 落盘校验失败")
            if binding is None:
                binding = NoteBinding(
                    knowledge_item_id=item.id,
                    zhiliu_id=item.id,
                    relative_path=relative_path,
                    content_hash=content_hash(written.body),
                    last_written_hash=content_hash(written.body),
                    sync_state="synced",
                    last_synced_at=datetime.now(timezone.utc),
                )
                session.add(binding)
                await session.flush()
            await self._apply_vault_version(
                session,
                item,
                binding,
                current.title,
                written.body,
                relative_path,
                source_metadata_json=current.source_metadata_json,
            )
        return item

    @staticmethod
    def _source_url(source_metadata_json: str) -> str | None:
        try:
            metadata = json.loads(source_metadata_json or "{}")
        except json.JSONDecodeError:
            return None
        value = metadata.get("url") if isinstance(metadata, dict) else None
        return value if isinstance(value, str) else None

    @staticmethod
    def _vault_source_metadata(
        item: KnowledgeItem,
        body: str,
        relative_path: str,
        *,
        source_url: str | None = None,
    ) -> str:
        normalized_body = normalize_content(body).rstrip("\n")
        metadata: dict[str, object] = {
            "source_type": item.source_type,
            "media_type": "text/markdown",
            "segments": [
                {
                    "text": normalized_body,
                    "locator": {"kind": "obsidian", "path": relative_path},
                }
            ],
        }
        if source_url:
            metadata["url"] = source_url
        return json.dumps(metadata, ensure_ascii=False)

    async def _apply_vault_version(
        self,
        session: AsyncSession,
        item: KnowledgeItem,
        binding: NoteBinding,
        title: str,
        body: str,
        relative_path: str,
        source_metadata_json: str | None = None,
    ) -> ContentVersion:
        if self.embedding_provider is None:
            raise ApplicationError(409, "embedding_not_configured", "Embedding capability 未配置")
        digest = content_hash(body)
        version = ContentVersion(
            knowledge_item_id=item.id,
            version_no=await self._next_version_no(session, item.id),
            source_kind="vault",
            title=title[:300],
            body=normalize_content(body),
            content_hash=digest,
            suggested_tags_json="[]",
            source_metadata_json=source_metadata_json
            or self._vault_source_metadata(item, body, relative_path),
        )
        session.add(version)
        await session.flush()
        index = IndexService(self.vector_store, self.embedding_provider)
        await index.index_version(session, item, version, relative_path)
        item.title = version.title
        item.content_hash = digest
        item.current_content_version_id = version.id
        item.status = "published"
        item.updated_at = datetime.now(timezone.utc)
        binding.relative_path = relative_path
        binding.content_hash = digest
        binding.last_written_hash = digest
        binding.sync_state = "synced"
        binding.last_synced_at = datetime.now(timezone.utc)
        binding.last_error = None
        return version

    async def rescan(self, minimum_file_age_seconds: float = 0) -> dict[str, int]:
        async with self._mutation_lock:
            return await self._rescan(minimum_file_age_seconds)

    async def _rescan(self, minimum_file_age_seconds: float) -> dict[str, int]:
        vault = self.vault()
        found: dict[str, list[tuple[str, object, bool]]] = {}
        present_relative_paths: set[str] = set()
        invalid = 0
        for path in vault.iter_markdown():
            relative = vault.relative_path(path)
            present_relative_paths.add(relative)
            try:
                before = path.stat()
                note = parse_note(path.read_text(encoding="utf-8"))
                after = path.stat()
            except (OSError, ValueError):
                invalid += 1
                continue
            if note.zhiliu_id:
                stable = (
                    before.st_mtime_ns == after.st_mtime_ns
                    and before.st_size == after.st_size
                    and (
                        minimum_file_age_seconds <= 0
                        or time.time() - after.st_mtime >= minimum_file_age_seconds
                    )
                )
                found.setdefault(note.zhiliu_id, []).append((relative, note, stable))
        changed = renamed = missing = conflicts = deferred = 0
        reconcile_versions: dict[str, str] = {}
        initial_reconciliation = not self._initial_vector_reconciliation_complete
        async with self.session_factory() as session, session.begin():
            bindings = list((await session.execute(select(NoteBinding))).scalars())
            bindings_by_id = {binding.zhiliu_id: binding for binding in bindings}
            for zhiliu_id, notes in found.items():
                binding = bindings_by_id.get(zhiliu_id)
                item = await session.get(KnowledgeItem, zhiliu_id)
                if len(notes) > 1:
                    conflicts += 1
                    if binding:
                        binding.sync_state = "conflict"
                        binding.last_error = "同一 zhiliu_id 出现在多个文件"
                    continue
                relative, note_object, stable = notes[0]
                note = note_object
                if item is None:
                    continue
                if binding is None:
                    binding = NoteBinding(
                        knowledge_item_id=item.id,
                        zhiliu_id=item.id,
                        relative_path=relative,
                        content_hash=content_hash(note.body),
                        sync_state="changed",
                    )
                    session.add(binding)
                    await session.flush()
                    bindings_by_id[zhiliu_id] = binding
                if binding.relative_path != relative:
                    binding.relative_path = relative
                    renamed += 1
                digest = content_hash(note.body)
                if digest != binding.content_hash:
                    if not stable:
                        binding.sync_state = "changed"
                        binding.last_error = None
                        deferred += 1
                        continue
                    title_value = note.metadata.get("title")
                    title = title_value if isinstance(title_value, str) else item.title
                    await self._apply_vault_version(
                        session, item, binding, title, note.body, relative
                    )
                    changed += 1
                    if item.current_content_version_id:
                        reconcile_versions[item.id] = item.current_content_version_id
                else:
                    binding.sync_state = "synced"
                    binding.last_synced_at = datetime.now(timezone.utc)
            for binding in bindings:
                if binding.zhiliu_id not in found:
                    if binding.relative_path in present_relative_paths:
                        binding.sync_state = "error"
                        binding.last_error = "Markdown 暂时无法解析，等待稳定后重试"
                        continue
                    binding.sync_state = "missing"
                    binding.last_error = "受管理 Markdown 文件不存在"
                    missing += 1
            if initial_reconciliation:
                published = (
                    await session.execute(
                        select(KnowledgeItem).where(
                            KnowledgeItem.status == "published",
                            KnowledgeItem.current_content_version_id.is_not(None),
                        )
                    )
                ).scalars()
                for item in published:
                    if item.current_content_version_id:
                        reconcile_versions[item.id] = item.current_content_version_id
        for item_id, version_id in reconcile_versions.items():
            self.vector_store.delete_item_except_version(item_id, version_id)
        if initial_reconciliation:
            self._initial_vector_reconciliation_complete = True
        return {
            "changed": changed,
            "renamed": renamed,
            "missing": missing,
            "conflicts": conflicts,
            "invalid": invalid,
            "deferred": deferred,
        }

    async def get_item(
        self, item_id: str
    ) -> tuple[KnowledgeItem, ContentVersion, NoteBinding | None]:
        async with self.session_factory() as session:
            item = await self._item(session, item_id)
            version = await self._version(session, item)
            binding_result = await session.execute(
                select(NoteBinding).where(NoteBinding.knowledge_item_id == item.id)
            )
            binding = binding_result.scalar_one_or_none()
            if item.status == "published" and binding is not None:
                try:
                    note = self.vault().read(binding.relative_path)
                except (OSError, ValueError):
                    # Editors can briefly expose an incomplete file while saving.
                    # Keep serving the last successfully indexed version until a
                    # later rescan observes a stable, valid Markdown document.
                    pass
                else:
                    version.body = note.body
                    version.content_hash = content_hash(note.body)
            return item, version, binding

    async def soft_delete(self, item_id: str) -> None:
        async with self._mutation_lock:
            async with self.session_factory() as session, session.begin():
                item = await self._item(session, item_id)
                item.status = "deleted"
                item.deleted_at = datetime.now(timezone.utc)
                item.updated_at = item.deleted_at
            self.vector_store.delete_item(item_id)

    async def _item(self, session: AsyncSession, item_id: str) -> KnowledgeItem:
        item = await session.get(KnowledgeItem, item_id)
        if item is None or item.deleted_at is not None:
            raise ApplicationError(404, "item_not_found", "知识条目不存在")
        return item

    async def _version(self, session: AsyncSession, item: KnowledgeItem) -> ContentVersion:
        if not item.current_content_version_id:
            raise ApplicationError(409, "content_not_ready", "内容版本尚未生成")
        version = await session.get(ContentVersion, item.current_content_version_id)
        if version is None or version.knowledge_item_id != item.id:
            raise ApplicationError(500, "content_version_invalid", "当前内容版本无效")
        return version
