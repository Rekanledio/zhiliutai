"""Safe public and internal contracts for the stage 6 workflow skeletons.

The workflow state is deliberately smaller than the data exchanged by the
existing application services.  Only identifiers, routing decisions, stable
error codes, and other JSON-safe primitives may cross the graph boundary.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal, Protocol, TypedDict, cast
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from app.core.safety import REDACTED, redact_sensitive_text


MAX_SAFE_QUERY_CHARS = 2_000

SchemaVersion = Literal["stage6-v1"]
SourceType = Literal["text", "markdown", "pdf", "docx", "webpage", "video"]
Decision = Literal["approve", "reject", "cancel"]
IngestionProcessStatus = Literal["ready", "asr_required"]

IngestionStage = Literal[
    "validate",
    "route_source",
    "process",
    "review_gate",
    "review_decision",
    "publish_gate",
    "publish_decision",
    "publish",
    "completed",
    "rejected",
    "cancelled",
    "failed",
]

EvidenceStatus = Literal["unknown", "none", "low_confidence", "sufficient"]
QuestionAnswerMode = Literal["answer", "search"]
QuestionAnswerRoute = Literal[
    "validate",
    "classify",
    "retrieve",
    "evidence_gate",
    "refuse",
    "answer",
    "completed",
    "failed",
]

ErrorCode = Literal[
    "ingestion_invalid_state",
    "ingestion_invalid_resume",
    "ingestion_process_failed",
    "ingestion_review_failed",
    "ingestion_publish_failed",
    "ingestion_invalid_result",
    "question_answer_invalid_state",
    "question_answer_query_failed",
    "question_answer_retrieve_failed",
    "question_answer_answer_failed",
    "question_answer_invalid_result",
]


def canonical_uuid(value: object) -> str:
    """Validate an identifier without allowing coercion or path-like input."""

    if not isinstance(value, str) or value != value.strip() or "\x00" in value:
        raise ValueError("ID must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("ID must be a canonical UUID") from error
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("ID must be a canonical UUID")
    return canonical


CanonicalId = Annotated[StrictStr, BeforeValidator(canonical_uuid)]


_CITATION_ID = re.compile(r"^C[1-9][0-9]{0,2}$")


def safe_citation_id(value: object) -> str:
    """Accept stable local citation labels or canonical persisted IDs only."""

    if not isinstance(value, str) or value != value.strip() or "\x00" in value:
        raise ValueError("citation ID is invalid")
    if _CITATION_ID.fullmatch(value):
        return value
    return canonical_uuid(value)


SafeCitationId = Annotated[StrictStr, BeforeValidator(safe_citation_id)]

_TRACEBACK_MARKER = re.compile(r"(?i)\b(?:traceback|stack\s+trace)\b")
_QUERY_SECRET_ASSIGNMENT = re.compile(
    r"(?is)(?<![A-Za-z0-9_-])(?:api[_ -]?key|authorization|bearer|cookie|set-cookie|"
    r"access[_ -]?token|refresh[_ -]?token|password|secret|token)(?![A-Za-z0-9_-])"
    r"\s*(?:[:=]|\s+bearer\s+)\s*[^\r\n]+"
)
_UNQUOTED_WINDOWS_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\)[^\r\n<>\"']+"
)


def sanitize_query(value: object, *, max_chars: int = MAX_SAFE_QUERY_CHARS) -> str:
    """Redact query secrets before the first graph checkpoint is created.

    Diagnostic traces are rejected because their surrounding text is not a
    useful question and can contain arbitrary exception data.  Credential
    assignments, cookies, and local paths are replaced before normalization.
    """

    if not isinstance(value, str):
        raise ValueError("query must be text")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ValueError("query contains unsupported control characters")
    if _TRACEBACK_MARKER.search(value):
        raise ValueError("query must not contain diagnostic traces")

    safe_value = redact_sensitive_text(value)
    safe_value = _QUERY_SECRET_ASSIGNMENT.sub(REDACTED, safe_value)
    safe_value = _UNQUOTED_WINDOWS_PATH.sub(REDACTED, safe_value)
    normalized = unicodedata.normalize("NFKC", safe_value)
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError("query must not be empty")
    if len(normalized) > max_chars:
        raise ValueError(f"query must not exceed {max_chars} characters")
    return normalized


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class IngestionInput(_ContractModel):
    schema_version: SchemaVersion = "stage6-v1"
    job_id: CanonicalId
    item_id: CanonicalId
    source_type: SourceType

    def to_state(self) -> IngestionState:
        return cast(
            IngestionState,
            {
                "schema_version": self.schema_version,
                "job_id": self.job_id,
                "item_id": self.item_id,
                "source_type": self.source_type,
                "stage": "validate",
                "review_decision": None,
                "publish_decision": None,
                "result_content_version_id": None,
                "processing_status": None,
                "error_code": None,
            },
        )


class IngestionResumeDecision(_ContractModel):
    decision: Decision


class IngestionProcessResult(_ContractModel):
    content_version_id: CanonicalId | None = None
    status: IngestionProcessStatus = "ready"

    @model_validator(mode="after")
    def _require_version_for_ready_result(self) -> IngestionProcessResult:
        if self.status == "ready" and self.content_version_id is None:
            raise ValueError("ready ingestion result requires a content version")
        if self.status == "asr_required" and self.content_version_id is not None:
            raise ValueError("asr_required ingestion result cannot contain a content version")
        return self


class IngestionPublishResult(_ContractModel):
    content_version_id: CanonicalId


class IngestionInterruptPayload(_ContractModel):
    kind: Literal["review_required", "publish_required"]
    stage: Literal["review_gate", "publish_gate"]
    job_id: CanonicalId
    item_id: CanonicalId
    source_type: SourceType
    result_content_version_id: CanonicalId | None = None
    decision_options: list[Decision] = Field(
        default_factory=lambda: ["approve", "reject", "cancel"]
    )


class IngestionStateModel(_ContractModel):
    schema_version: SchemaVersion = "stage6-v1"
    job_id: CanonicalId
    item_id: CanonicalId
    source_type: SourceType
    stage: IngestionStage
    review_decision: Decision | None = None
    publish_decision: Decision | None = None
    result_content_version_id: CanonicalId | None = None
    processing_status: IngestionProcessStatus | None = None
    error_code: ErrorCode | None = None

    def to_state(self) -> IngestionState:
        return cast(IngestionState, self.model_dump(mode="json"))


class QuestionAnswerInput(_ContractModel):
    request_id: CanonicalId
    safe_query: StrictStr = Field(
        min_length=1,
        max_length=MAX_SAFE_QUERY_CHARS,
        validation_alias=AliasChoices("safe_query", "query"),
    )
    mode: QuestionAnswerMode = "answer"

    @field_validator("safe_query", mode="before")
    @classmethod
    def _sanitize_safe_query(cls, value: object) -> str:
        return sanitize_query(value)

    def to_state(self) -> QuestionAnswerState:
        return cast(
            QuestionAnswerState,
            {
                "schema_version": "stage6-v1",
                "request_id": self.request_id,
                "safe_query": self.safe_query,
                "mode": self.mode,
                "normalized_query": None,
                "evidence_status": "unknown",
                "route": "validate",
                "model_run_id": None,
                "citation_ids": [],
                "refusal_code": None,
                "error_code": None,
            },
        )


class QuestionAnswerRetrievalResult(_ContractModel):
    evidence_status: EvidenceStatus
    citation_ids: list[SafeCitationId] = Field(default_factory=list, max_length=100)


class QuestionAnswerAnswerResult(_ContractModel):
    model_run_id: CanonicalId
    citation_ids: list[SafeCitationId] = Field(default_factory=list, max_length=100)


class QuestionAnswerStateModel(_ContractModel):
    schema_version: SchemaVersion = "stage6-v1"
    request_id: CanonicalId
    safe_query: StrictStr = Field(min_length=1, max_length=MAX_SAFE_QUERY_CHARS)
    mode: QuestionAnswerMode
    normalized_query: StrictStr | None = Field(default=None, max_length=MAX_SAFE_QUERY_CHARS)
    evidence_status: EvidenceStatus
    route: QuestionAnswerRoute
    model_run_id: CanonicalId | None = None
    citation_ids: list[SafeCitationId] = Field(default_factory=list, max_length=100)
    refusal_code: Literal["insufficient_evidence"] | None = None
    error_code: ErrorCode | None = None

    @field_validator("safe_query", mode="before")
    @classmethod
    def _state_query_is_safe(cls, value: object) -> str:
        return sanitize_query(value)

    @field_validator("normalized_query", mode="before")
    @classmethod
    def _state_normalized_query_is_safe(cls, value: object) -> str | None:
        return None if value is None else sanitize_query(value)

    def to_state(self) -> QuestionAnswerState:
        return cast(QuestionAnswerState, self.model_dump(mode="json"))


class IngestionState(TypedDict, total=False):
    schema_version: SchemaVersion
    job_id: str
    item_id: str
    source_type: SourceType
    stage: IngestionStage
    review_decision: Decision | None
    publish_decision: Decision | None
    result_content_version_id: str | None
    processing_status: IngestionProcessStatus | None
    error_code: ErrorCode | None


class QuestionAnswerState(TypedDict, total=False):
    schema_version: SchemaVersion
    request_id: str
    safe_query: str
    mode: QuestionAnswerMode
    normalized_query: str | None
    evidence_status: EvidenceStatus
    route: QuestionAnswerRoute
    model_run_id: str | None
    citation_ids: list[str]
    refusal_code: str | None
    error_code: ErrorCode | None


class IngestionWorkflowServices(Protocol):
    async def process(
        self, *, job_id: str, item_id: str, source_type: SourceType
    ) -> IngestionProcessResult: ...

    async def review(
        self, *, job_id: str, item_id: str, decision: Decision
    ) -> None: ...

    async def abandon(
        self, *, job_id: str, item_id: str, decision: Decision
    ) -> None: ...

    async def publish(
        self, *, job_id: str, item_id: str, content_version_id: str
    ) -> IngestionPublishResult: ...


class QuestionAnswerWorkflowServices(Protocol):
    async def retrieve(
        self,
        *,
        request_id: str,
        safe_query: str,
        normalized_query: str,
        mode: QuestionAnswerMode,
    ) -> QuestionAnswerRetrievalResult: ...

    async def answer(
        self,
        *,
        request_id: str,
        safe_query: str,
        normalized_query: str,
        citation_ids: list[str],
    ) -> QuestionAnswerAnswerResult: ...
