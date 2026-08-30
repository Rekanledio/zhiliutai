"""Thin production adapters for the two existing ingestion boundaries.

The adapter owns workflow coordination and checkpoint identity.  Stage2Service
and VideoService remain the owners of acquisition, parsing, drafting, Vault
publishing, and index mutation rules.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError
from app.core.safety import redact_sensitive_text
from app.db.models import ContentVersion, KnowledgeItem, ProcessingJob
from app.services.stage2 import Stage2Service
from app.workflows.contracts import (
    Decision,
    IngestionInput,
    IngestionProcessResult,
    IngestionPublishResult,
    IngestionResumeDecision,
    IngestionWorkflowServices,
    canonical_uuid,
)
from app.workflows.runtime import WorkflowRun, WorkflowRuntime, thread_config


class WorkflowJobError(RuntimeError):
    """A stable job failure that never exposes service/provider text."""

    def __init__(
        self,
        code: str,
        *,
        public_message: str | None = None,
        public_type: str | None = None,
    ) -> None:
        self.code = code
        self.public_message = public_message
        self.public_type = public_type
        super().__init__(code)


_SAFE_JOB_RESULT_KEYS = frozenset(
    {
        "item_id",
        "source_type",
        "status",
        "asr_called",
        "subtitle_available",
        "transcript_artifact_id",
        "media_artifact_id",
        "subtitle_artifact_id",
        "snapshot_artifact_id",
        "chapter_count",
        "visual_event_count",
        "source_url_hash",
        "content_version_id",
        "workflow_stage",
    }
)
_SAFE_ID_KEYS = frozenset(
    {
        "item_id",
        "transcript_artifact_id",
        "media_artifact_id",
        "subtitle_artifact_id",
        "snapshot_artifact_id",
        "content_version_id",
    }
)
def _json_object(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise WorkflowJobError("ingestion_invalid_state") from error
    if not isinstance(parsed, dict):
        raise WorkflowJobError("ingestion_invalid_state")
    return parsed


def _safe_job_result(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, object] = {}
    for key, item in value.items():
        if key not in _SAFE_JOB_RESULT_KEYS or not isinstance(key, str):
            continue
        if key in _SAFE_ID_KEYS:
            try:
                safe[key] = canonical_uuid(item)
            except ValueError:
                continue
        elif key in {"chapter_count", "visual_event_count"}:
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 100_000:
                safe[key] = item
        elif key == "asr_called" or key == "subtitle_available":
            if isinstance(item, bool):
                safe[key] = item
        elif isinstance(item, str) and len(item) <= 200:
            safe[key] = item
    return safe


def _safe_failure(error: BaseException) -> dict[str, str]:
    code = getattr(error, "code", "job_failed")
    if not isinstance(code, str) or not code.isascii() or not code.replace("_", "").isalnum():
        code = "job_failed"
    message = redact_sensitive_text(str(error)).replace("\r", " ").replace("\n", " ").strip()
    if not message or "traceback" in message.casefold() or "stack trace" in message.casefold():
        message = "处理失败"
    return {
        "code": code[:80],
        "message": message[:300],
        "type": type(error).__name__[:100],
    }


class Stage2IngestionWorkflowServices(IngestionWorkflowServices):
    """Adapt existing Stage2/Video methods without moving their business rules."""

    def __init__(
        self,
        stage2: Stage2Service,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.stage2 = stage2
        self.session_factory = session_factory
        self._process_results: dict[str, dict[str, object]] = {}
        self._failures: dict[str, dict[str, str]] = {}

    def take_process_result(self, job_id: str) -> dict[str, object]:
        return self._process_results.pop(job_id, {})

    def take_failure(self, job_id: str) -> dict[str, str] | None:
        return self._failures.pop(job_id, None)

    async def _job_and_item(
        self, job_id: str, item_id: str, source_type: str | None = None
    ) -> tuple[ProcessingJob, KnowledgeItem, dict[str, object]]:
        try:
            canonical_job_id = canonical_uuid(job_id)
            canonical_item_id = canonical_uuid(item_id)
        except ValueError as error:
            raise WorkflowJobError("ingestion_invalid_state") from error
        async with self.session_factory() as session:
            job = await session.get(ProcessingJob, canonical_job_id)
            item = await session.get(KnowledgeItem, canonical_item_id)
        if job is None or item is None or item.deleted_at is not None:
            raise WorkflowJobError("ingestion_invalid_state")
        payload = _json_object(job.payload_json)
        if payload.get("item_id") != item.id or (
            source_type is not None and item.source_type != source_type
        ):
            raise WorkflowJobError("ingestion_invalid_state")
        return job, item, payload

    async def _already_processed_version(
        self, item: KnowledgeItem
    ) -> str | None:
        candidate_id = item.pending_content_version_id
        if candidate_id is None and item.status == "pending_review":
            candidate_id = item.current_content_version_id
        if candidate_id is None:
            return None
        async with self.session_factory() as session:
            version = await session.get(ContentVersion, candidate_id)
        if version is None or version.knowledge_item_id != item.id:
            raise WorkflowJobError("ingestion_invalid_state")
        return version.id

    async def process(
        self, *, job_id: str, item_id: str, source_type: str
    ) -> IngestionProcessResult:
        job, item, _payload = await self._job_and_item(job_id, item_id, source_type)
        existing_version = await self._already_processed_version(item)
        if existing_version is not None:
            result = IngestionProcessResult(content_version_id=existing_version)
            self._process_results[job.id] = {"content_version_id": existing_version}
            return result

        try:
            if source_type == "video":
                raw_result = await self.stage2.process_video(job)
            else:
                raw_result = await self.stage2.process_ingestion(job)
        except Exception as error:
            self._failures[job.id] = _safe_failure(error)
            raise
        if not isinstance(raw_result, Mapping):
            raise WorkflowJobError("ingestion_invalid_result")
        safe_summary = _safe_job_result(raw_result)
        self._process_results[job.id] = safe_summary
        if raw_result.get("status") == "asr_required":
            return IngestionProcessResult(status="asr_required")
        try:
            result = IngestionProcessResult(
                content_version_id=raw_result.get("content_version_id")
            )
        except ValidationError as error:
            raise WorkflowJobError("ingestion_invalid_result") from error
        return result

    async def review(
        self, *, job_id: str, item_id: str, decision: Decision
    ) -> None:
        _job, item, _payload = await self._job_and_item(job_id, item_id)
        if decision != "approve":
            await self.stage2.abandon_pending_decision(item.id, decision)
            return
        if item.status in {"reviewed", "published"}:
            return
        if item.status != "pending_review":
            raise WorkflowJobError("ingestion_invalid_state")
        await self.stage2.review(item.id)

    async def abandon(
        self, *, job_id: str, item_id: str, decision: Decision
    ) -> None:
        if decision == "approve":
            raise WorkflowJobError("ingestion_invalid_decision")
        _job, item, _payload = await self._job_and_item(job_id, item_id)
        await self.stage2.abandon_pending_decision(item.id, decision)

    async def publish(
        self, *, job_id: str, item_id: str, content_version_id: str
    ) -> IngestionPublishResult:
        try:
            canonical_expected = canonical_uuid(content_version_id)
        except ValueError as error:
            raise WorkflowJobError("ingestion_invalid_result") from error
        _job, item, _payload = await self._job_and_item(job_id, item_id)
        if item.status == "published" and item.pending_content_version_id is None:
            current = item.current_content_version_id
            if current is None:
                raise WorkflowJobError("ingestion_invalid_state")
            return IngestionPublishResult(content_version_id=current)
        if item.status not in {"reviewed", "published"}:
            raise WorkflowJobError("ingestion_invalid_state")
        expected_before_publish = item.pending_content_version_id or item.current_content_version_id
        if expected_before_publish != canonical_expected:
            raise WorkflowJobError("ingestion_invalid_result")
        published_item = await self.stage2.publish(item.id)
        current = published_item.current_content_version_id
        if current is None:
            raise WorkflowJobError("ingestion_invalid_result")
        try:
            result = IngestionPublishResult(content_version_id=current)
        except ValidationError as error:
            raise WorkflowJobError("ingestion_invalid_result") from error
        return result


class IngestionWorkflowCoordinator:
    """Connect JobRunner and existing review/publish routes to one graph thread."""

    def __init__(
        self,
        runtime: WorkflowRuntime,
        services: Stage2IngestionWorkflowServices,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.runtime = runtime
        self.services = services
        self.session_factory = session_factory

    @staticmethod
    def _input_for_job(job: ProcessingJob) -> IngestionInput:
        try:
            payload = _json_object(job.payload_json)
            return IngestionInput(
                job_id=job.id,
                item_id=payload.get("item_id"),
                source_type=payload.get("source_type"),
            )
        except (ValidationError, WorkflowJobError) as error:
            raise WorkflowJobError("ingestion_invalid_state") from error

    @staticmethod
    def _has_checkpoint(snapshot: Any) -> bool:
        return bool(snapshot.values) or bool(snapshot.next) or bool(snapshot.interrupts)

    async def _run_job_graph(self, job: ProcessingJob, input_model: IngestionInput) -> WorkflowRun:
        config = thread_config(job.id)
        snapshot = await self.runtime.ingestion_graph.aget_state(config)
        if not self._has_checkpoint(snapshot):
            return await self.runtime.run_ingestion(input_model, thread_id=job.id)
        current = await self.runtime.snapshot_ingestion(job.id)
        if current.interrupted:
            return current
        if current.get("stage") == "failed":
            return await self.runtime.retry_ingestion(job.id)
        if current.get("stage") in {"completed", "rejected", "cancelled"}:
            return current
        return await self.runtime.continue_ingestion(job.id)

    @staticmethod
    def _interrupt_stage(run: WorkflowRun) -> str | None:
        interrupts = run.result.get("__interrupt__")
        if not isinstance(interrupts, list) or not interrupts:
            return None
        first = interrupts[0]
        return first.get("stage") if isinstance(first, Mapping) else None

    def _job_result(self, job: ProcessingJob, run: WorkflowRun) -> dict[str, object]:
        result = _safe_job_result(_json_object(job.result_json))
        result.update(_safe_job_result(self.services.take_process_result(job.id)))
        result.update(
            {
                "item_id": run.result.get("item_id", result.get("item_id")),
                "source_type": run.result.get("source_type", result.get("source_type")),
                "workflow_stage": run.result.get("stage"),
            }
        )
        return {key: value for key, value in result.items() if value is not None}

    async def run_job(self, job: ProcessingJob) -> dict[str, object]:
        input_model = self._input_for_job(job)
        run = await self._run_job_graph(job, input_model)
        result = self._job_result(job, run)
        interrupt_stage = self._interrupt_stage(run)
        if interrupt_stage == "review_gate":
            result.update({"_job_stage": "pending_review", "_job_progress": 1.0})
        elif interrupt_stage == "publish_gate":
            result.update({"_job_stage": "pending_publish", "_job_progress": 1.0})
        elif run.get("stage") == "completed":
            if run.get("processing_status") == "asr_required":
                result.update(
                    {"status": "asr_required", "asr_called": False, "_job_stage": "asr_required"}
                )
            else:
                result["content_version_id"] = run.get("result_content_version_id")
                result["_job_stage"] = "complete"
            result["_job_progress"] = 1.0
        elif run.get("stage") in {"rejected", "cancelled"}:
            result.update({"_job_stage": run.get("stage"), "_job_progress": 1.0, "_job_state": "cancelled"})
        elif run.get("stage") == "failed":
            failure = self.services.take_failure(job.id)
            if failure is not None:
                raise WorkflowJobError(
                    failure["code"],
                    public_message=failure["message"],
                    public_type=failure["type"],
                )
            raise WorkflowJobError(str(run.get("error_code") or "ingestion_invalid_state"))
        else:
            raise WorkflowJobError("ingestion_invalid_state")
        return result

    async def _find_jobs(self, item_id: str) -> list[ProcessingJob]:
        try:
            canonical_item_id = canonical_uuid(item_id)
        except ValueError as error:
            raise ApplicationError(404, "item_not_found", "知识条目不存在") from error
        async with self.session_factory() as session:
            jobs = list(
                (
                    await session.execute(
                        select(ProcessingJob)
                        .where(ProcessingJob.kind.in_(["ingest_text", "ingest_source", "ingest_video"]))
                        .order_by(ProcessingJob.created_at.desc())
                    )
                ).scalars()
            )
        matched: list[ProcessingJob] = []
        for job in jobs:
            try:
                payload = _json_object(job.payload_json)
            except WorkflowJobError:
                continue
            if payload.get("item_id") == canonical_item_id:
                matched.append(job)
        return matched

    async def _record_run(self, job_id: str, run: WorkflowRun) -> ProcessingJob:
        result = _safe_job_result(run.result)
        async with self.session_factory() as session, session.begin():
            job = await session.get(ProcessingJob, job_id)
            if job is None:
                raise ApplicationError(404, "job_not_found", "任务不存在")
            merged = _safe_job_result(_json_object(job.result_json))
            merged.update(result)
            interrupt_stage = self._interrupt_stage(run)
            if interrupt_stage == "review_gate":
                job.state = "succeeded"
                job.stage = "pending_review"
                job.progress = 1.0
            elif interrupt_stage == "publish_gate":
                job.state = "succeeded"
                job.stage = "pending_publish"
                job.progress = 1.0
            elif run.get("stage") == "completed":
                job.state = "succeeded"
                job.stage = "asr_required" if run.get("processing_status") == "asr_required" else "complete"
                job.progress = 1.0
            elif run.get("stage") in {"rejected", "cancelled"}:
                job.state = "cancelled"
                job.stage = str(run.get("stage"))
                job.progress = 1.0
            elif run.get("stage") == "failed":
                job.state = "failed"
                job.stage = "failed"
                job.error_json = json.dumps(
                    {"code": run.get("error_code") or "ingestion_invalid_state", "message": "工作流执行失败"},
                    ensure_ascii=False,
                )
            else:
                raise ApplicationError(409, "workflow_invalid_state", "工作流状态无效")
            job.result_json = json.dumps(merged, ensure_ascii=False)
            return job

    async def resume_item(
        self, item_id: str, decision: IngestionResumeDecision, *, gate: str
    ) -> WorkflowRun:
        jobs = await self._find_jobs(item_id)
        waiting_stage = "pending_review" if gate == "review" else "pending_publish"
        job = next(
            (candidate for candidate in jobs if candidate.stage == waiting_stage and candidate.state != "cancelled"),
            None,
        )
        if job is not None:
            run = await self.runtime.resume_ingestion(job.id, decision)
            await self._record_run(job.id, run)
            return run

        latest = jobs[0] if jobs else None
        if latest is not None:
            latest_run = await self.runtime.snapshot_ingestion(latest.id)
            completed_stage = latest_run.get("stage")
            if gate == "review" and (
                (completed_stage == "publish_gate" and decision.decision == "approve")
                or (completed_stage == "rejected" and decision.decision == "reject")
                or (completed_stage == "cancelled" and decision.decision == "cancel")
            ):
                return latest_run
            if gate == "publish" and (
                (completed_stage == "completed" and decision.decision == "approve")
                or (completed_stage == "rejected" and decision.decision == "reject")
                or (completed_stage == "cancelled" and decision.decision == "cancel")
            ):
                return latest_run

        failed = next((candidate for candidate in jobs if candidate.state == "failed"), None)
        if failed is not None:
            run = await self.runtime.retry_ingestion(failed.id)
            if run.interrupted and (
                (gate == "review" and self._interrupt_stage(run) == "review_gate")
                or (gate == "publish" and self._interrupt_stage(run) == "publish_gate")
            ):
                run = await self.runtime.resume_ingestion(failed.id, decision)
            await self._record_run(failed.id, run)
            return run
        raise ApplicationError(409, "workflow_not_waiting", "工作流当前不在指定审核或发布节点")

    async def cancel_job(self, job_id: str) -> ProcessingJob | None:
        try:
            canonical_job_id = canonical_uuid(job_id)
        except ValueError as error:
            raise ApplicationError(404, "job_not_found", "任务不存在") from error
        async with self.session_factory() as session:
            job = await session.get(ProcessingJob, canonical_job_id)
        if job is None:
            raise ApplicationError(404, "job_not_found", "任务不存在")
        if job.stage not in {"pending_review", "pending_publish"} or job.state == "cancelled":
            return None
        run = await self.runtime.resume_ingestion(
            canonical_job_id,
            IngestionResumeDecision(decision="cancel"),
        )
        return await self._record_run(canonical_job_id, run)
