from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Chunk, ContentVersion, KnowledgeItem
from app.providers.models import EmbeddingProvider
from app.rag.query import ProcessedQuery, QueryProcessor
from app.rag.reranking import RerankerError, RerankerProvider
from app.rag.types import RetrievedChunk, RetrievalDiagnostics
from app.services.content import content_hash
from app.services.vector_store import QdrantLocalStore


class RetrievalError(RuntimeError):
    """Both retrieval channels failed for an otherwise valid query."""


StableChunkKey = tuple[str, str, int, str, str, str, str]


def _stable_chunk_key(
    item_content_hash: object,
    version_content_hash: object,
    ordinal: object,
    chunk_content_hash: object,
    source_type: object,
    source_locator: object,
    content: object,
) -> StableChunkKey:
    return (
        str(item_content_hash or ""),
        str(version_content_hash or ""),
        int(ordinal),
        str(chunk_content_hash or ""),
        str(source_type or ""),
        str(source_locator or ""),
        str(content or ""),
    )


@dataclass(frozen=True)
class ChannelHit:
    chunk_id: str
    rank: int
    score: float | None = None
    stable_key: StableChunkKey | None = None


@dataclass(frozen=True)
class FusedHit:
    chunk_id: str
    matched_by: tuple[str, ...]
    fts_rank: int | None
    vector_rank: int | None
    fts_score: float | None
    vector_score: float | None
    rrf_score: float


def _coerce_channel_hit(value: ChannelHit | str, rank: int) -> ChannelHit:
    if isinstance(value, ChannelHit):
        return value
    if isinstance(value, str):
        return ChannelHit(chunk_id=value, rank=rank)
    raise TypeError("召回结果必须是 ChannelHit 或 chunk_id")


def reciprocal_rank_fusion(
    channels: Mapping[str, Sequence[ChannelHit | str]] | Sequence[ChannelHit | str],
    vector_hits: Sequence[ChannelHit | str] | None = None,
    *,
    k: int = 60,
    weights: Mapping[str, float] | None = None,
) -> list[FusedHit]:
    """Fuse ranked channel hits by weighted reciprocal rank."""

    if k <= 0:
        raise ValueError("RRF k 必须为正数")
    if isinstance(channels, Mapping):
        channel_map = channels
    else:
        channel_map = {"fts": channels}
        if vector_hits is not None:
            channel_map["vector"] = vector_hits

    fused: dict[str, dict[str, Any]] = {}
    for channel_name, values in channel_map.items():
        weight = float((weights or {}).get(channel_name, 1.0))
        if weight <= 0:
            continue
        for index, raw_hit in enumerate(values, start=1):
            hit = _coerce_channel_hit(raw_hit, index)
            if hit.rank <= 0:
                raise ValueError("召回 rank 必须从 1 开始")
            item = fused.setdefault(
                hit.chunk_id,
                {
                    "matched_by": set(),
                    "rrf_score": 0.0,
                    "ranks": {},
                    "scores": {},
                },
            )
            item["matched_by"].add(channel_name)
            item["rrf_score"] += weight / (k + hit.rank)
            item["ranks"][channel_name] = min(
                hit.rank, item["ranks"].get(channel_name, hit.rank)
            )
            if hit.stable_key is not None:
                current_key = item.get("stable_key")
                if current_key is None or hit.stable_key < current_key:
                    item["stable_key"] = hit.stable_key
            if hit.score is not None:
                item["scores"][channel_name] = hit.score

    def channel_order(name: str) -> tuple[int, str]:
        return (0 if name == "fts" else 1 if name == "vector" else 2, name)

    def key(pair: tuple[str, dict[str, Any]]) -> tuple[float, int, int, StableChunkKey, str]:
        chunk_id, item = pair
        best_rank = min(item["ranks"].values()) if item["ranks"] else 2**31 - 1
        stable_key = item.get("stable_key")
        if stable_key is None:
            return (
                -item["rrf_score"],
                best_rank,
                1,
                ("", "", 2**31 - 1, "", "", "", ""),
                chunk_id,
            )
        return (-item["rrf_score"], best_rank, 0, stable_key, "")

    output: list[FusedHit] = []
    for chunk_id, item in sorted(fused.items(), key=key):
        matched_by = tuple(sorted(item["matched_by"], key=channel_order))
        output.append(
            FusedHit(
                chunk_id=chunk_id,
                matched_by=matched_by,
                fts_rank=item["ranks"].get("fts"),
                vector_rank=item["ranks"].get("vector"),
                fts_score=item["scores"].get("fts"),
                vector_score=item["scores"].get("vector"),
                rrf_score=float(item["rrf_score"]),
            )
        )
    return output


@dataclass(frozen=True)
class EvidenceAssessment:
    status: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


class EvidencePolicy:
    """Classifies retrieval evidence without treating RRF as probability."""

    def __init__(
        self,
        *,
        vector_score_threshold: float = 0.35,
        fts_confident_rank: int = 3,
    ) -> None:
        self.vector_score_threshold = vector_score_threshold
        self.fts_confident_rank = fts_confident_rank

    def assess(self, chunks: Sequence[RetrievedChunk]) -> EvidenceAssessment:
        if not chunks:
            return EvidenceAssessment("none", "没有通过当前版本校验的证据")
        for chunk in chunks:
            if chunk.fts_rank is not None and chunk.fts_rank <= self.fts_confident_rank:
                return EvidenceAssessment("sufficient", "存在高位全文命中")
            if chunk.fts_rank is not None and chunk.vector_rank is not None:
                return EvidenceAssessment("sufficient", "同一 Chunk 被全文和向量共同命中")
            if (
                chunk.vector_score is not None
                and chunk.vector_score >= self.vector_score_threshold
            ):
                return EvidenceAssessment("sufficient", "向量相似度达到证据阈值")
        return EvidenceAssessment("low_confidence", "只有低置信度单路命中")


@dataclass(frozen=True)
class _CurrentVersion:
    item_id: str
    version_id: str
    source_type: str


@dataclass(frozen=True)
class _VectorCandidate:
    chunk_id: str
    content_version_id: str
    knowledge_item_id: str
    source_type: str
    source_locator: str
    embedding_model: str
    embedding_version: str
    score: float
    stable_key: StableChunkKey | None = None


class HybridRetriever:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        vector_store: QdrantLocalStore,
        embedding_provider: EmbeddingProvider | None,
        settings: Settings | None = None,
        query_processor: QueryProcessor | None = None,
        reranker: RerankerProvider | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self.settings = settings
        self.reranker = reranker
        self.query_processor = query_processor or QueryProcessor(
            (settings.rag_query_max_chars if settings else 2_000)
        )
        self.evidence_policy = EvidencePolicy(
            vector_score_threshold=(settings.rag_vector_score_threshold if settings else 0.35),
            fts_confident_rank=(settings.rag_fts_confident_rank if settings else 3),
        )

    async def _apply_reranker(
        self,
        query: str,
        chunks: Sequence[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        if self.reranker is None or not chunks:
            return list(chunks)
        configured_limit = self.settings.rag_rerank_limit if self.settings else len(chunks)
        candidate_count = min(len(chunks), max(1, configured_limit))
        candidates = list(chunks[:candidate_count])
        raw_scores = await self.reranker.rerank(query, candidates)
        if not isinstance(raw_scores, Mapping):
            raise RerankerError("reranker 返回结构无效")
        scores: dict[str, float] = {}
        for chunk in candidates:
            raw_score = raw_scores.get(chunk.chunk_id)
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise RerankerError("reranker 未返回完整分数")
            score = float(raw_score)
            if not math.isfinite(score):
                raise RerankerError("reranker 返回了无效分数")
            scores[chunk.chunk_id] = score
        if len(scores) != len(candidates):
            raise RerankerError("reranker 返回了重复 Chunk")
        ranked = sorted(
            enumerate(candidates),
            key=lambda pair: (-scores[pair[1].chunk_id], pair[0], pair[1].chunk_id),
        )
        reranked = [
            replace(chunk, rerank_score=scores[chunk.chunk_id])
            for _, chunk in ranked
        ]
        return reranked + list(chunks[candidate_count:])

    async def _current_versions(
        self, source_types: Sequence[str] | None
    ) -> list[_CurrentVersion]:
        async with self.session_factory() as session:
            statement = select(
                KnowledgeItem.id,
                KnowledgeItem.current_content_version_id,
                KnowledgeItem.source_type,
            ).where(
                KnowledgeItem.status == "published",
                KnowledgeItem.deleted_at.is_(None),
                KnowledgeItem.current_content_version_id.is_not(None),
            )
            if source_types:
                statement = statement.where(KnowledgeItem.source_type.in_(source_types))
            rows = (await session.execute(statement)).all()
        return [
            _CurrentVersion(
                item_id=str(row.id),
                version_id=str(row.current_content_version_id),
                source_type=str(row.source_type),
            )
            for row in rows
            if row.current_content_version_id
        ]

    async def _search_fts(
        self,
        processed: ProcessedQuery,
        limit: int,
        source_types: Sequence[str] | None,
    ) -> list[ChannelHit]:
        if not processed.fts_query:
            return []
        parameters: dict[str, object] = {"match_query": processed.fts_query, "limit": limit}
        source_clause = ""
        if source_types:
            names: list[str] = []
            for index, source_type in enumerate(source_types):
                name = f"source_type_{index}"
                names.append(f":{name}")
                parameters[name] = source_type
            source_clause = f"AND k.source_type IN ({', '.join(names)})"
        statement = text(
            "SELECT c.id AS chunk_id, "
            "k.content_hash AS item_content_hash, "
            "v.content_hash AS version_content_hash, "
            "c.ordinal AS ordinal, c.content_hash AS chunk_content_hash, "
            "c.source_type AS source_type, c.source_locator AS source_locator, "
            "c.content AS content, bm25(chunk_fts) AS bm25_score "
            "FROM chunk_fts "
            "JOIN chunks AS c ON c.id = chunk_fts.chunk_id "
            "JOIN knowledge_items AS k ON k.id = c.knowledge_item_id "
            "JOIN content_versions AS v ON v.id = c.content_version_id "
            "WHERE chunk_fts MATCH :match_query "
            "AND c.knowledge_item_id = k.id "
            "AND v.knowledge_item_id = k.id "
            "AND k.status = 'published' "
            "AND k.deleted_at IS NULL "
            "AND k.current_content_version_id = c.content_version_id "
            f"{source_clause} "
            "ORDER BY bm25_score ASC, k.content_hash ASC, v.content_hash ASC, "
            "c.ordinal ASC, c.content_hash ASC, c.source_type ASC, "
            "c.source_locator ASC, c.content ASC "
            "LIMIT :limit"
        )
        async with self.session_factory() as session:
            rows = (await session.execute(statement, parameters)).mappings().all()
        return [
            ChannelHit(
                chunk_id=str(row["chunk_id"]),
                rank=index,
                score=float(row["bm25_score"]),
                stable_key=_stable_chunk_key(
                    row["item_content_hash"],
                    row["version_content_hash"],
                    row["ordinal"],
                    row["chunk_content_hash"],
                    row["source_type"],
                    row["source_locator"],
                    row["content"],
                ),
            )
            for index, row in enumerate(rows, start=1)
        ]

    @staticmethod
    def _validate_vector_candidate(raw: dict[str, object]) -> _VectorCandidate | None:
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return None
        required = (
            "chunk_id",
            "knowledge_item_id",
            "content_version_id",
            "source_type",
            "source_locator",
            "embedding_model",
            "embedding_version",
        )
        if any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
            return None
        score = raw.get("score")
        if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            return None
        return _VectorCandidate(
            chunk_id=payload["chunk_id"],
            content_version_id=payload["content_version_id"],
            knowledge_item_id=payload["knowledge_item_id"],
            source_type=payload["source_type"],
            source_locator=payload["source_locator"],
            embedding_model=payload["embedding_model"],
            embedding_version=payload["embedding_version"],
            score=float(score),
        )

    async def _validate_vector_candidates(
        self,
        candidates: Sequence[_VectorCandidate],
        source_types: Sequence[str] | None,
    ) -> list[_VectorCandidate]:
        if not candidates:
            return []
        candidate_ids = list(dict.fromkeys(candidate.chunk_id for candidate in candidates))
        async with self.session_factory() as session:
            statement = (
                select(Chunk, ContentVersion, KnowledgeItem)
                .join(ContentVersion, ContentVersion.id == Chunk.content_version_id)
                .join(KnowledgeItem, KnowledgeItem.id == Chunk.knowledge_item_id)
                .where(
                    Chunk.id.in_(candidate_ids),
                    Chunk.knowledge_item_id == KnowledgeItem.id,
                    ContentVersion.knowledge_item_id == KnowledgeItem.id,
                    KnowledgeItem.status == "published",
                    KnowledgeItem.deleted_at.is_(None),
                    KnowledgeItem.current_content_version_id == ContentVersion.id,
                    KnowledgeItem.current_content_version_id == Chunk.content_version_id,
                )
            )
            if source_types:
                statement = statement.where(KnowledgeItem.source_type.in_(source_types))
            rows = (await session.execute(statement)).all()
        authoritative = {chunk.id: (chunk, version, item) for chunk, version, item in rows}
        valid: list[_VectorCandidate] = []
        for candidate in candidates:
            row = authoritative.get(candidate.chunk_id)
            if row is None:
                continue
            chunk, version, item = row
            if (
                candidate.knowledge_item_id != item.id
                or candidate.content_version_id != version.id
                or candidate.source_type != item.source_type
                or candidate.source_type != chunk.source_type
                or candidate.source_locator != chunk.source_locator
                or candidate.embedding_model != chunk.embedding_model
                or candidate.embedding_version != chunk.embedding_version
            ):
                continue
            valid.append(
                replace(
                    candidate,
                    stable_key=_stable_chunk_key(
                        item.content_hash,
                        version.content_hash,
                        chunk.ordinal,
                        chunk.content_hash,
                        chunk.source_type,
                        chunk.source_locator,
                        chunk.content,
                    ),
                )
            )
        return valid

    async def _search_vector(
        self,
        query: str,
        current_versions: Sequence[_CurrentVersion],
        limit: int,
        source_types: Sequence[str] | None,
    ) -> list[ChannelHit]:
        if self.embedding_provider is None or not current_versions:
            return []
        vectors = await self.embedding_provider.embed([query])
        if len(vectors) != 1:
            raise ValueError("Embedding 查询返回数量不一致")
        raw_results = await asyncio.to_thread(
            self.vector_store.search,
            vectors[0],
            limit,
            content_version_ids=[item.version_id for item in current_versions],
            source_types=source_types,
        )
        raw_candidates: list[_VectorCandidate] = []
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            candidate = self._validate_vector_candidate(raw)
            if candidate is None:
                continue
            raw_candidates.append(candidate)
        candidates = await self._validate_vector_candidates(raw_candidates, source_types)
        candidates = sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                candidate.stable_key
                or ("", "", 2**31 - 1, "", "", "", candidate.chunk_id),
            ),
        )
        return [
            ChannelHit(candidate.chunk_id, rank, candidate.score, candidate.stable_key)
            for rank, candidate in enumerate(candidates, start=1)
        ]

    async def _rehydrate(
        self,
        hits: Sequence[FusedHit],
        source_types: Sequence[str] | None,
    ) -> list[RetrievedChunk]:
        if not hits:
            return []
        chunk_ids = [hit.chunk_id for hit in hits]
        async with self.session_factory() as session:
            statement = (
                select(Chunk, ContentVersion, KnowledgeItem)
                .join(ContentVersion, ContentVersion.id == Chunk.content_version_id)
                .join(KnowledgeItem, KnowledgeItem.id == Chunk.knowledge_item_id)
                .where(
                    Chunk.id.in_(chunk_ids),
                    Chunk.knowledge_item_id == KnowledgeItem.id,
                    ContentVersion.knowledge_item_id == KnowledgeItem.id,
                    KnowledgeItem.status == "published",
                    KnowledgeItem.deleted_at.is_(None),
                    KnowledgeItem.current_content_version_id == Chunk.content_version_id,
                )
            )
            if source_types:
                statement = statement.where(KnowledgeItem.source_type.in_(source_types))
            rows = (await session.execute(statement)).all()
        authoritative = {chunk.id: (chunk, version, item) for chunk, version, item in rows}
        output: list[RetrievedChunk] = []
        for hit in hits:
            row = authoritative.get(hit.chunk_id)
            if row is None:
                continue
            chunk, version, item = row
            output.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    knowledge_item_id=item.id,
                    content_version_id=version.id,
                    item_title=item.title,
                    version_no=version.version_no,
                    source_type=chunk.source_type,
                    content=chunk.content,
                    source_locator=chunk.source_locator,
                    ordinal=chunk.ordinal,
                    content_hash=chunk.content_hash or content_hash(chunk.content),
                    matched_by=hit.matched_by,
                    fts_rank=hit.fts_rank,
                    vector_rank=hit.vector_rank,
                    fts_score=hit.fts_score,
                    vector_score=hit.vector_score,
                    rrf_score=hit.rrf_score,
                )
            )
        return output

    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 6,
        source_types: Sequence[str] | None = None,
    ) -> tuple[list[RetrievedChunk], RetrievalDiagnostics, EvidenceAssessment]:
        if not 1 <= limit <= 100:
            raise ValueError("检索条数必须在 1 到 100 之间")
        processed = self.query_processor.process(query)
        current_versions = await self._current_versions(source_types)
        fts_limit = max(limit, self.settings.rag_fts_limit if self.settings else 30)
        vector_limit = max(limit, self.settings.rag_vector_limit if self.settings else 30)

        channel_errors: dict[str, str] = {}
        fts_task = asyncio.create_task(
            self._search_fts(processed, fts_limit, source_types)
        )
        vector_task = asyncio.create_task(
            self._search_vector(
                processed.normalized, current_versions, vector_limit, source_types
            )
        )
        fts_result, vector_result = await asyncio.gather(
            fts_task, vector_task, return_exceptions=True
        )
        fts_hits: list[ChannelHit] = []
        vector_hits: list[ChannelHit] = []
        if isinstance(fts_result, Exception):
            channel_errors["fts"] = type(fts_result).__name__
        else:
            fts_hits = fts_result
        if isinstance(vector_result, Exception):
            channel_errors["vector"] = type(vector_result).__name__
        else:
            vector_hits = vector_result

        fts_available = "fts" not in channel_errors
        vector_available = self.embedding_provider is not None and "vector" not in channel_errors
        if channel_errors and len(channel_errors) == 2:
            raise RetrievalError("全文和向量检索均不可用")
        fused = reciprocal_rank_fusion(
            {"fts": fts_hits, "vector": vector_hits},
            k=self.settings.rag_rrf_k if self.settings else 60,
        )
        chunks = await self._rehydrate(fused, source_types)
        if self.reranker is not None:
            try:
                chunks = await self._apply_reranker(processed.normalized, chunks)
            except Exception as error:
                channel_errors["reranker"] = type(error).__name__
        selected_chunks = chunks[:limit]
        assessment = self.evidence_policy.assess(selected_chunks)
        diagnostics = RetrievalDiagnostics(
            original_query=processed.original,
            normalized_query=processed.normalized,
            fts_query=processed.fts_query,
            fts_available=fts_available,
            vector_available=vector_available,
            degraded=bool(channel_errors),
            channel_errors=channel_errors,
            reranker_available=(
                self.reranker is not None and "reranker" not in channel_errors
            ),
        )
        return selected_chunks, diagnostics, assessment

    async def validate_current(self, chunks: Sequence[RetrievedChunk]) -> bool:
        """Confirm every selected chunk still belongs to its current item version."""
        if not chunks:
            return False
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        async with self.session_factory() as session:
            statement = (
                select(Chunk.id)
                .join(ContentVersion, ContentVersion.id == Chunk.content_version_id)
                .join(KnowledgeItem, KnowledgeItem.id == Chunk.knowledge_item_id)
                .where(
                    Chunk.id.in_(chunk_ids),
                    Chunk.knowledge_item_id == KnowledgeItem.id,
                    ContentVersion.knowledge_item_id == KnowledgeItem.id,
                    KnowledgeItem.status == "published",
                    KnowledgeItem.deleted_at.is_(None),
                    KnowledgeItem.current_content_version_id == Chunk.content_version_id,
                )
            )
            current_ids = set((await session.execute(statement)).scalars())
        return current_ids == set(chunk_ids)
