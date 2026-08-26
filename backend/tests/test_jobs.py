import json
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import JobAttempt, ProcessingJob
from app.db.session import create_engine
from app.services.jobs import JobRunner
from conftest import migrate


@pytest.mark.asyncio
async def test_job_failure_retry_and_attempt_history(settings, monkeypatch) -> None:
    await asyncio.to_thread(migrate, settings, monkeypatch)
    engine = create_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    calls = 0

    async def handler(_job: ProcessingJob) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first attempt fails")
        return {"ok": True}

    runner = JobRunner(factory, {"test": handler})
    async with factory() as session, session.begin():
        job = ProcessingJob(kind="test", payload_json="{}")
        session.add(job)
        await session.flush()
        job_id = job.id
    assert await runner.run_once() is True
    async with factory() as session:
        failed = await session.get(ProcessingJob, job_id)
        assert failed is not None
        assert failed.state == "failed"
        assert json.loads(failed.error_json or "{}")["type"] == "RuntimeError"
    await runner.retry(job_id)
    assert await runner.run_once() is True
    async with factory() as session:
        succeeded = await session.get(ProcessingJob, job_id)
        attempts = list(
            (
                await session.execute(
                    select(JobAttempt)
                    .where(JobAttempt.processing_job_id == job_id)
                    .order_by(JobAttempt.attempt_no)
                )
            ).scalars()
        )
        assert succeeded is not None and succeeded.state == "succeeded"
        assert [(attempt.attempt_no, attempt.state) for attempt in attempts] == [
            (1, "failed"),
            (2, "succeeded"),
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_job_restart_recovery_requeues_and_closes_attempt(
    settings, monkeypatch
) -> None:
    await asyncio.to_thread(migrate, settings, monkeypatch)
    engine = create_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        job = ProcessingJob(kind="test", state="running", stage="embedding", payload_json="{}")
        session.add(job)
        await session.flush()
        session.add(
            JobAttempt(
                processing_job_id=job.id,
                attempt_no=1,
                state="running",
                stage="embedding",
            )
        )
        job_id = job.id
    runner = JobRunner(factory, {"test": lambda _job: _job})
    assert await runner.recover_interrupted() == 1
    async with factory() as session:
        recovered = await session.get(ProcessingJob, job_id)
        attempt = (
            await session.execute(
                select(JobAttempt).where(JobAttempt.processing_job_id == job_id)
            )
        ).scalar_one()
        assert recovered is not None
        assert recovered.state == "queued"
        assert recovered.retry_count == 1
        assert attempt.state == "failed"
        assert json.loads(attempt.error_json or "{}")["code"] == "process_restarted"
    await engine.dispose()
