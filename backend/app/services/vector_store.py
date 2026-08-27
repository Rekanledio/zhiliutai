from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from qdrant_client import QdrantClient, models


COLLECTION_NAME = "knowledge_chunks"


@dataclass(frozen=True)
class VectorRecord:
    point_id: str
    vector: list[float]
    chunk_id: str
    knowledge_item_id: str
    content_version_id: str
    source_type: str
    source_locator: str
    embedding_model: str
    embedding_version: str

    def payload(self) -> dict[str, str]:
        return {
            "chunk_id": self.chunk_id,
            "knowledge_item_id": self.knowledge_item_id,
            "content_version_id": self.content_version_id,
            "source_type": self.source_type,
            "source_locator": self.source_locator,
            "embedding_model": self.embedding_model,
            "embedding_version": self.embedding_version,
        }


class QdrantLocalStore:
    def __init__(self, path: Path, dimensions: int) -> None:
        self.path = path
        self.dimensions = dimensions

    def _client(self) -> QdrantClient:
        self.path.mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=str(self.path))

    def ensure_collection(self) -> None:
        client = self._client()
        try:
            if not client.collection_exists(COLLECTION_NAME):
                client.create_collection(
                    collection_name=COLLECTION_NAME,
                    vectors_config=models.VectorParams(
                        size=self.dimensions, distance=models.Distance.COSINE
                    ),
                )
        finally:
            client.close()

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        self.ensure_collection()
        client = self._client()
        try:
            client.upsert(
                collection_name=COLLECTION_NAME,
                wait=True,
                points=[
                    models.PointStruct(
                        id=record.point_id,
                        vector=record.vector,
                        payload=record.payload(),
                    )
                    for record in records
                ],
            )
        finally:
            client.close()

    def delete_version(self, content_version_id: str) -> None:
        self._delete_filter(
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="content_version_id",
                        match=models.MatchValue(value=content_version_id),
                    )
                ]
            )
        )

    def delete_item(self, knowledge_item_id: str) -> None:
        self._delete_filter(
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="knowledge_item_id",
                        match=models.MatchValue(value=knowledge_item_id),
                    )
                ]
            )
        )

    def delete_item_except_version(self, knowledge_item_id: str, current_version_id: str) -> None:
        self._delete_filter(
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="knowledge_item_id",
                        match=models.MatchValue(value=knowledge_item_id),
                    )
                ],
                must_not=[
                    models.FieldCondition(
                        key="content_version_id",
                        match=models.MatchValue(value=current_version_id),
                    )
                ],
            )
        )

    def _delete_filter(self, point_filter: models.Filter) -> None:
        self.ensure_collection()
        client = self._client()
        try:
            client.delete(
                collection_name=COLLECTION_NAME,
                wait=True,
                points_selector=models.FilterSelector(filter=point_filter),
            )
        finally:
            client.close()

    def search(self, vector: list[float], limit: int = 5) -> list[dict[str, object]]:
        self.ensure_collection()
        client = self._client()
        try:
            result = client.query_points(
                collection_name=COLLECTION_NAME,
                query=vector,
                limit=limit,
                with_payload=True,
            )
            return [
                {"id": str(point.id), "score": point.score, "payload": point.payload or {}}
                for point in result.points
            ]
        finally:
            client.close()
