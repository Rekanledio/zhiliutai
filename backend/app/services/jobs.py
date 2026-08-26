import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError
from app.db.models import JobAttempt, ProcessingJob

JobHandler = Callable[[ProcessingJob], Awaitable[dict[str, object]]]
logger = structlog.get_logger("jobs")


class JobRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: dict[str, JobHandler],
        poll_seconds: float = 0.2,
    ) -> None:
        self.session_factory = session_factory
        self.handlers = handlers
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()

    async def recover_interrupted(self) -> int:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                select(ProcessingJob).where(ProcessingJob.state == "running")
            )
            jobs = list(result.scalars())
            for job in jobs:
                if job.retry_count >= job.max_retries:
                    job.state = "failed"
                    job.stage = "retry_limit_reached"
                    job.finished_at = now
                else:
                    job.retry_count += 1
                    job.state = "queued"
                    job.stage = "recovered_after_restart"
                job.heartbeat_at = now
            await session.execute(
                update(JobAttempt)
                .where(JobAttempt.state == "running")
                .values(
                    state="failed",
                    finished_at=now,
                    error_json=json.dumps(
                        {"code": "process_restarted", "message": "进程重启，任务已重新排队"},
                        ensure_ascii=False,
                    ),
                )
            )
            return len(jobs)

    async def run_once(self) -> bool:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                select(ProcessingJob)
                .where(ProcessingJob.state == "queued")
                .order_by(ProcessingJob.created_at)
                .limit(1)
            )
            job = result.scalar_one_or_none()
            if job is None:
                return False
            job.state = "running"
            job.stage = "starting"
            job.started_at = job.started_at or now
            job.heartbeat_at = now
            attempt = JobAttempt(
                processing_job_id=job.id,
                attempt_no=job.retry_count + 1,
                state="running",
                stage="starting",
                heartbeat_at=now,
            )
            session.add(attempt)
            job_id = job.id
            job_kind = job.kind
            await session.flush()
            session.expunge(job)

        handler = self.handlers.get(job_kind)
        error_payload: dict[str, str] | None = None
        result_payload: dict[str, object] | None = None
        try:
            if handler is None:
                raise RuntimeError(f"Unknown job kind: {job_kind}")
            result_payload = await handler(job)
        except Exception as error:
            logger.exception(
                "job_failed", job_id=job_id, job_kind=job_kind, error_type=type(error).__name__
            )
            error_payload = {
                "code": getattr(error, "code", "job_failed"),
                "message": str(error),
                "type": type(error).__name__,
            }

        finished = datetime.now(timezone.utc)
        async with self.session_factory() as session, session.begin():
            job = await session.get(ProcessingJob, job_id)
            if job is None:
                return True
            attempt_result = await session.execute(
                select(JobAttempt)
                .where(
                    JobAttempt.processing_job_id == job_id,
                    JobAttempt.attempt_no == job.retry_count + 1,
                )
                .limit(1)
            )
            attempt = attempt_result.scalar_one()
            if error_payload is None:
                job.state = "succeeded"
                job.stage = "complete"
                job.progress = 1.0
                job.result_json = json.dumps(result_payload or {}, ensure_ascii=False)
                job.error_json = None
                attempt.state = "succeeded"
                attempt.stage = "complete"
            else:
                encoded = json.dumps(error_payload, ensure_ascii=False)
                job.state = "failed"
                job.stage = "failed"
                job.error_json = encoded
                attempt.state = "failed"
                attempt.stage = "failed"
                attempt.error_json = encoded
            job.finished_at = finished
            job.heartbeat_at = finished
            attempt.finished_at = finished
            attempt.heartbeat_at = finished
        return True

    async def retry(self, job_id: str) -> ProcessingJob:
        async with self.session_factory() as session, session.begin():
            job = await session.get(ProcessingJob, job_id)
            if job is None:
                raise ApplicationError(404, "job_not_found", "任务不存在")
            if job.state != "failed":
                raise ApplicationError(409, "job_not_retryable", "只有失败任务可以重试")
            if job.retry_count >= job.max_retries:
                raise ApplicationError(409, "retry_limit_reached", "任务已达到重试上限")
            job.retry_count += 1
            job.state = "queued"
            job.stage = "queued"
            job.progress = 0.0
            job.error_json = None
            job.finished_at = None
            await session.flush()
            return job

    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            try:
                worked = await self.run_once()
            except Exception as error:
                logger.info("job_runner_poll_failed", error_type=type(error).__name__)
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()
