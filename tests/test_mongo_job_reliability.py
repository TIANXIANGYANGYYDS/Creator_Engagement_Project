"""Opt-in integration tests use uniquely named collections, never production rows."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.engagement import CreateJobRequest, JobItemResponse
from app.repositories.job_repository import DuplicateIdempotencyKeyError, StoredJob
from app.repositories.mongo_job_repository import MongoJobRepository
from app.services.job_service import BatchJobManager

pytestmark = pytest.mark.skipif(
    os.environ.get("CREATOR_RUN_MONGO_TESTS") != "1",
    reason="opt-in MongoDB integration test",
)


@pytest.fixture
def repository():
    settings = Settings()
    repo = MongoJobRepository(settings.mongo_uri, settings.mongo_db_name)
    prefix = f"reliability_test_{uuid4().hex}_"
    repo.jobs = repo.database[prefix + "jobs"]
    repo.results = repo.database[prefix + "results"]
    repo.requests = repo.database[prefix + "requests"]
    try:
        yield repo
    finally:
        for collection in (repo.jobs, repo.results, repo.requests):
            collection.drop()
        repo.client.close()


def input_and_job(job_id="job-test", key=None):
    request = CreateJobRequest(
        items=[
            {
                "item_id": str(index),
                "url": f"https://www.douyin.com/video/{1000 + index}",
                "media_name": "抖音",
                "type": "interactions",
            }
            for index in range(3)
        ]
    )
    job = StoredJob(
        job_id=job_id,
        fingerprint="fingerprint",
        status="running",
        total=3,
        completed=0,
        success=0,
        failed=0,
        duration_ms=0,
        cost_yuan=0,
        created_at=datetime.now(timezone.utc),
        idempotency_key=key,
    )
    return request, job


def successful(item):
    return JobItemResponse(
        **item.model_dump(), status="success", complete=True, result={"likes": 2}
    )


@asynccontextmanager
async def no_cost():
    yield SimpleNamespace(added_endpoint_count=0)


def test_mongo_restart_recovers_input_and_progress_after_uncommitted_counter(
    repository,
):
    async def scenario():
        await repository.initialize(60)
        request, job = input_and_job()
        await repository.create_job(job, request)
        await repository.append_result(job.job_id, 0, successful(request.items[0]))
        # Simulate a crash after a result insert but before the job counter update.
        manager = BatchJobManager(repository=repository, item_max_concurrency=1)
        seen = []

        async def collector(item):
            seen.append(item.item_id)
            return successful(item)

        await manager.resume_interrupted(collector=collector, proxy_usage_scope=no_cost)
        await asyncio.wait_for(asyncio.gather(*manager._tasks.values()), 3)
        status = await manager.get_status(job.job_id)
        assert status.status == "completed"
        assert status.progress.success == 3
        assert seen == ["1", "2"]
        assert [r.item_id for r in await repository.get_results(job.job_id, 0, 10)] == [
            "0",
            "1",
            "2",
        ]
        assert (
            await repository.get_request(job.job_id)
        ).model_dump() == request.model_dump()
        assert (
            repository.requests.count_documents({"expires_at": {"$type": "date"}}) == 3
        )
        await manager._webhook_client.aclose()

    asyncio.run(scenario())


def test_mongo_duplicate_idempotency_does_not_leave_or_delete_inputs(repository):
    async def scenario():
        await repository.initialize(60)
        request, first = input_and_job("job-first", "same-key")
        _, second = input_and_job("job-second", "same-key")
        await repository.create_job(first, request)
        with pytest.raises(DuplicateIdempotencyKeyError):
            await repository.create_job(second, request)
        assert repository.requests.count_documents({"job_id": "job-first"}) == 3
        assert repository.requests.count_documents({"job_id": "job-second"}) == 0
        assert repository.jobs.count_documents({}) == 1

    asyncio.run(scenario())


def test_mongo_resume_clears_expiry_on_results_and_inputs(repository):
    async def scenario():
        await repository.initialize(60)
        request, job = input_and_job()
        await repository.create_job(job, request)
        await repository.append_result(job.job_id, 0, successful(request.items[0]))
        job.status = "failed"
        job.expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        await repository.update_job(job)
        assert (
            repository.requests.count_documents({"expires_at": {"$type": "date"}}) == 3
        )
        job.status = "queued"
        job.expires_at = None
        await repository.update_job(job)
        assert repository.requests.count_documents({"expires_at": None}) == 3
        assert repository.results.count_documents({"expires_at": None}) == 1
        # Legacy jobs with incomplete inputs must never be resumed with invented data.
        repository.requests.delete_one({"sequence": 1})
        assert await repository.get_request(job.job_id) is None

    asyncio.run(scenario())
