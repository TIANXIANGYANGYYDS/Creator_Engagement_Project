from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.api.routers.jobs import job_collector
from app.models.engagement import CreateJobRequest, InteractionResult, JobItemResponse
from app.repositories.job_repository import MemoryJobRepository, SQLiteJobRepository
from app.services.engagement_service import EngagementService, PlatformTrafficPolicy
from app.services.job_service import BatchJobManager


def request_with_items(count: int) -> CreateJobRequest:
    return CreateJobRequest(
        items=[
            {
                "item_id": f"item-{index}",
                "url": f"https://www.douyin.com/video/{1000 + index}",
                "media_name": "抖音",
                "type": "interactions",
            }
            for index in range(count)
        ]
    )


def successful(item) -> JobItemResponse:
    return JobItemResponse(
        **item.model_dump(), status="success", complete=True, result={"likes": 1}
    )


@asynccontextmanager
async def no_proxy_cost():
    yield SimpleNamespace(added_endpoint_count=0)


async def finish(manager: BatchJobManager, job_id: str):
    task = manager._tasks.get(job_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 3)
    return await manager.get_status(job_id)


def test_batch_deadline_scales_with_input_size() -> None:
    async def scenario():
        manager = BatchJobManager(
            item_max_concurrency=1, job_timeout_seconds=0.1, timeout_batch_size=2
        )

        async def collector(item):
            await asyncio.sleep(0.03)
            return successful(item)

        try:
            submitted = await manager.submit(
                request_with_items(5),
                collector=collector,
                proxy_usage_scope=no_proxy_cost,
            )
            status = await finish(manager, submitted.job_id)
            assert status.status == "completed"
            assert status.progress.success == 5
            assert status.duration_ms >= 150
        finally:
            await manager.aclose()

    asyncio.run(scenario())


def test_account_pacing_budget_is_specific_to_xiaohongshu_and_accepts_aliases():
    from app.models.engagement import JobItemRequest
    async def scenario():
        manager = BatchJobManager(item_timeout_seconds=0.01,
                                  platform_item_timeout_seconds={"xiaohongshu": 0.15})
        async def collector(item):
            await asyncio.sleep(0.03)
            return successful(item)
        try:
            for media, expected in [("小红书", "success"), ("xhs", "success"), ("抖音", "failed")]:
                item = JobItemRequest(item_id=media, url="https://example.com/1", media_name=media, type="interactions")
                result = await manager._collect_one(item, collector, None)
                assert result.status == expected
                if expected == "failed":
                    assert "0.01 秒" in result.error
        finally:
            await manager.aclose()
    asyncio.run(scenario())


class CapacityCrawler:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.calls = 0

    async def _wait_for_circuit_cooldown(self, platform):
        pass

    async def fetch_interactions(self, url, media_name):
        self.active += 1
        self.calls += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            return InteractionResult(
                platform="douyin",
                canonical_url=url,
                work_id="1",
                coverage="partial",
                stats={"likes": 1},
            )
        finally:
            self.active -= 1

    async def aclose(self):
        pass


def test_unknown_platform_in_mixed_batch_keeps_validation_error():
    async def scenario():
        crawler = CapacityCrawler()
        service = EngagementService(crawler)
        manager = BatchJobManager()
        request = request_with_items(2)
        request.items[0].media_name = "尚未接入的来源"
        try:
            submitted = await manager.submit(
                request, collector=job_collector(service),
                proxy_usage_scope=no_proxy_cost,
                collection_scope=lambda item: service.collection_slot(item.media_name),
            )
            status = await finish(manager, submitted.job_id)
            results = await manager.get_results(submitted.job_id, cursor=None, limit=10)
            assert status.progress.success == status.progress.failed == 1
            failed = next(item for item in results.data if item.status == "failed")
            assert "unsupported media_name" in failed.error
            assert failed.media_name == "尚未接入的来源"
            assert crawler.calls == 1
        finally:
            await manager.aclose()
            await service.aclose()
    asyncio.run(scenario())


def test_queued_batch_items_get_full_execution_budget_without_exceeding_capacity() -> (
    None
):
    async def scenario():
        crawler = CapacityCrawler()
        service = EngagementService(
            crawler,
            collection_max_concurrency=1,
            platform_policies={"douyin": PlatformTrafficPolicy(1, 0)},
        )
        manager = BatchJobManager(item_max_concurrency=8, item_timeout_seconds=0.06)
        try:
            submitted = await manager.submit(
                request_with_items(8),
                collector=job_collector(service),
                proxy_usage_scope=no_proxy_cost,
                collection_scope=lambda item: service.collection_slot(item.media_name),
            )
            status = await finish(manager, submitted.job_id)
            assert status.progress.success == 8
            assert crawler.peak == 1
            assert crawler.calls == 8
        finally:
            await manager.aclose()
            await service.aclose()

    asyncio.run(scenario())


def test_account_backlog_does_not_starve_other_platforms():
    async def scenario():
        manager = BatchJobManager(item_max_concurrency=2, item_timeout_seconds=0.1,
                                  platform_item_max_concurrency={"xiaohongshu": 1})
        release_account = asyncio.Event()
        other_finished = asyncio.Event()
        active = peak = 0

        @asynccontextmanager
        async def scope(item):
            if item.media_name in {"小红书", "xhs"}:
                await release_account.wait()
            yield

        async def collector(item):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                if item.media_name == "抖音":
                    other_finished.set()
                return successful(item)
            finally:
                active -= 1

        request = request_with_items(8)
        for i, item in enumerate(request.items[:6]):
            item.media_name = "小红书" if i % 2 else "xhs"
        try:
            submitted = await manager.submit(request, collector=collector,
                proxy_usage_scope=no_proxy_cost, collection_scope=scope)
            # Old FIFO workers both waited on the account and never reached Douyin.
            await asyncio.wait_for(other_finished.wait(), 0.5)
            assert not release_account.is_set()
            release_account.set()
            status = await finish(manager, submitted.job_id)
            results = await manager.get_results(submitted.job_id, cursor=None, limit=20)
            assert status.progress.success == 8
            assert len({item.item_id for item in results.data}) == 8
            assert peak <= 2
        finally:
            release_account.set()
            await manager.aclose()
    asyncio.run(scenario())


def test_reserved_request_does_not_deadlock_with_unreserved_cache_waiter() -> None:
    async def scenario():
        crawler = CapacityCrawler()
        service = EngagementService(
            crawler,
            collection_max_concurrency=1,
            platform_policies={"douyin": PlatformTrafficPolicy(1, 0)},
        )
        url = "https://www.douyin.com/video/123"
        try:
            async with service.collection_slot("抖音"):
                # Drop the reservation in this caller, as a separate HTTP request would.
                token = service._reserved_platform.set(None)
                normal = asyncio.create_task(service.fetch_interactions(url, "抖音"))
                service._reserved_platform.reset(token)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                result = await asyncio.wait_for(
                    service.fetch_interactions(url, "抖音"), 0.5
                )
                assert result.stats.likes == 1
            assert (await asyncio.wait_for(normal, 0.5)).stats.likes == 1
            assert crawler.peak == 1
        finally:
            await service.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_restart_resumes_only_unfinished_inputs_and_retains_result_order(
    tmp_path, backend
) -> None:
    async def scenario():
        repository = (
            MemoryJobRepository()
            if backend == "memory"
            else SQLiteJobRepository(tmp_path / "jobs.db")
        )
        manager = BatchJobManager(repository=repository, item_max_concurrency=1)
        waiting = asyncio.Event()
        called = []

        async def interrupted(item):
            called.append(item.item_id)
            if item.item_id != "item-0":
                waiting.set()
                await asyncio.Event().wait()
            return successful(item)

        submitted = await manager.submit(
            request_with_items(3),
            collector=interrupted,
            proxy_usage_scope=no_proxy_cost,
        )
        await asyncio.wait_for(waiting.wait(), 1)
        await manager.aclose()
        if backend == "sqlite":
            repository = SQLiteJobRepository(tmp_path / "jobs.db")
        restored = BatchJobManager(repository=repository, item_max_concurrency=1)
        resumed = []

        async def collector(item):
            resumed.append(item.item_id)
            return successful(item)

        try:
            await restored.resume_interrupted(
                collector=collector, proxy_usage_scope=no_proxy_cost
            )
            status = await finish(restored, submitted.job_id)
            page = await restored.get_results(submitted.job_id, cursor=None, limit=10)
            assert status.status == "completed"
            assert status.progress.success == status.progress.total == 3
            assert resumed == ["item-1", "item-2"]
            assert [row.item_id for row in page.data] == ["item-0", "item-1", "item-2"]
            assert (
                await repository.get_request(submitted.job_id)
            ).model_dump() == request_with_items(3).model_dump()
        finally:
            await restored.aclose()

    asyncio.run(scenario())


def test_cancelled_job_is_not_automatically_resumed_but_can_be_explicitly_resumed() -> (
    None
):
    async def scenario():
        repository = MemoryJobRepository()
        manager = BatchJobManager(repository=repository)
        entered = asyncio.Event()

        async def blocked(item):
            entered.set()
            await asyncio.Event().wait()

        submitted = await manager.submit(
            request_with_items(1), collector=blocked, proxy_usage_scope=no_proxy_cost
        )
        await entered.wait()
        await manager.cancel(submitted.job_id)
        await manager.aclose()
        restored = BatchJobManager(repository=repository)

        async def collector(item):
            return successful(item)

        try:
            await restored.resume_interrupted(
                collector=collector, proxy_usage_scope=no_proxy_cost
            )
            assert not restored._tasks
            status = await restored.get_status(submitted.job_id)
            assert status.status == "cancelled"
            await restored.resume(
                submitted.job_id, collector=collector, proxy_usage_scope=no_proxy_cost
            )
            assert (await finish(restored, submitted.job_id)).progress.success == 1
        finally:
            await restored.aclose()

    asyncio.run(scenario())


def test_cancellation_finishes_inflight_result_write_before_restart() -> None:
    class DelayedRepository(MemoryJobRepository):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def append_result(self, job_id, sequence, result):
            self.entered.set()
            await self.release.wait()
            await super().append_result(job_id, sequence, result)

    async def scenario():
        repository = DelayedRepository()
        manager = BatchJobManager(repository=repository)

        async def collector(item):
            return successful(item)

        submitted = await manager.submit(
            request_with_items(1), collector=collector, proxy_usage_scope=no_proxy_cost
        )
        await repository.entered.wait()
        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        repository.release.set()
        await asyncio.wait_for(closing, 1)
        status = await repository.get_job(submitted.job_id)
        assert status.completed == 1
        assert len(await repository.get_results(submitted.job_id, 0, 10)) == 1

    asyncio.run(scenario())


def test_platform_pacing_wait_does_not_reserve_global_capacity():
    async def scenario():
        service = EngagementService(CapacityCrawler(), collection_max_concurrency=1,
            platform_policies={'xiaohongshu':PlatformTrafficPolicy(1,1), 'douyin':PlatformTrafficPolicy(1,0)})
        pacing_started = asyncio.Event()
        release = asyncio.Event()
        class Limiter:
            async def acquire(self):
                pacing_started.set()
                await release.wait()
        service._platform_rate_limiters[('xiaohongshu','comments')] = Limiter()
        async def slow():
            async with service.collection_slot('小红书','comments'):
                pass
        task = asyncio.create_task(slow())
        try:
            await pacing_started.wait()
            async with asyncio.timeout(.5):
                async with service.collection_slot('抖音','interactions'):
                    assert not release.is_set()
            release.set()
            await task
        finally:
            release.set()
            await task
            await service.aclose()
    asyncio.run(scenario())


def test_publisher_aliases_share_concurrency_budget():
    async def scenario():
        service = EngagementService(CapacityCrawler(),collection_max_concurrency=8,
            platform_policies={'sohu':PlatformTrafficPolicy(2,0),'sohu_news':PlatformTrafficPolicy(2,0)})
        active = peak = 0
        async def collect(media):
            nonlocal active,peak
            async with service.collection_slot(media,'interactions'):
                active+=1
                peak=max(peak,active)
                await asyncio.sleep(.001)
                active-=1
        try:
            await asyncio.gather(*(collect('搜狐号' if i%2 else '搜狐网') for i in range(12)))
            assert peak==2
        finally:await service.aclose()
    asyncio.run(scenario())
