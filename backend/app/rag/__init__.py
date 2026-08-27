"""Retrieval and answer orchestration primitives for the stage 4 RAG flow."""

from app.rag.query import ProcessedQuery, QueryProcessor
from app.rag.reranking import (
    KeywordOverlapReranker,
    RerankerError,
    RerankerProvider,
)
from app.rag.retrieval import (
    EvidenceAssessment,
    EvidencePolicy,
    HybridRetriever,
    RetrievalError,
    reciprocal_rank_fusion,
)
from app.rag.types import RetrievedChunk, RetrievalDiagnostics

__all__ = [
    "EvidenceAssessment",
    "EvidencePolicy",
    "HybridRetriever",
    "ProcessedQuery",
    "KeywordOverlapReranker",
    "QueryProcessor",
    "RerankerError",
    "RerankerProvider",
    "RetrievedChunk",
    "RetrievalDiagnostics",
    "RetrievalError",
    "reciprocal_rank_fusion",
]
