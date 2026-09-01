from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_engagement_service, get_job_manager
from app.models.engagement import (
    CommentPageResult,
    CreateJobRequest,
    EngagementComment,
    InteractionResult,
    JobItemResponse,
)
from app.services.job_service import BatchJobManager


class FakeJobService:
    @asynccontextmanager
    async def proxy_usage_scope(self):
        yield SimpleNamespace(added_endpoint_count=2)

    async def wait_for_platform_ready(self, media_name: str) -> None:
        return None

    async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
        if "failed" in url:
            raise ValueError("测试失败")
        return InteractionResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
            stats={"likes": 7},
        )

    async def fetch_comments(
        self,
        url: str,
        media_name: str,
        page: int,
        *,
        cursor: str | None = None,
    ) -> CommentPageResult:
        return CommentPageResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
            page=page,
            comments=[EngagementComment(comment_id=f"c-{page}", text=media_name)],
            total_comments=1,
            capabilities={
                "root_comments": "all_public_pages",
                "anonymous": True,
            },
        )


def _wait_for_job(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        payload = client.get(f"/api/v1/jobs/{job_id}").json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_async_job_submit_poll_and_paginate_results() -> None:
    app = create_app()
    service = FakeJobService()
    manager = BatchJobManager(retention_seconds=60, max_concurrency=1)
    app.dependency_overrides[get_engagement_service] = lambda: service
    app.dependency_overrides[get_job_manager] = lambda: manager
    payload = {
        "items": [
            {
                "item_id": "business-1",
                "url": "https://www.toutiao.com/article/1/",
                "media_name": "头条",
                "type": "interactions",
            },
            {
                "item_id": "business-2",
                "url": "https://www.toutiao.com/article/2/",
                "media_name": "toutiao",
                "type": "comments",
                "page": 1,
            },
        ]
    }

    with TestClient(app) as client:
        submitted = client.post(
            "/api/v1/jobs",
            json=payload,
            headers={"Idempotency-Key": "batch-001"},
        )
        repeated = client.post(
            "/api/v1/jobs",
            json=payload,
            headers={"Idempotency-Key": "batch-001"},
        )
        job_id = submitted.json()["job_id"]
        status = _wait_for_job(client, job_id)
        first_page = client.get(
            f"/api/v1/jobs/{job_id}/results",
            params={"limit": 1},
        )
        second_page = client.get(
            f"/api/v1/jobs/{job_id}/results",
            params={"cursor": first_page.json()["next_cursor"], "limit": 1},
        )
        terminal_cancel = client.post(f"/api/v1/jobs/{job_id}/cancel")

    assert submitted.status_code == 202
    assert repeated.status_code == 202
    assert repeated.json()["job_id"] == job_id
    assert status["status"] == "completed"
    assert status["progress"] == {
        "total": 2,
        "completed": 2,
        "success": 2,
        "failed": 0,
    }
    assert status["cost_yuan"] == 0.00168
    assert first_page.status_code == 200
    assert first_page.json()["available_count"] == 2
    assert first_page.json()["next_cursor"] is not None
    assert second_page.json()["next_cursor"] is None
    assert terminal_cancel.status_code == 200
    assert terminal_cancel.json()["status"] == "completed"
    results = first_page.json()["data"] + second_page.json()["data"]
    assert {item["item_id"] for item in results} == {"business-1", "business-2"}
    assert all(item["media_name"] == "今日头条" for item in results)
    assert all(item["status"] == "success" for item in results)
    asyncio.run(manager.aclose())


def test_async_job_validation_and_item_failure_are_isolated() -> None:
    app = create_app()
    service = FakeJobService()
    manager = BatchJobManager(retention_seconds=60)
    app.dependency_overrides[get_engagement_service] = lambda: service
    app.dependency_overrides[get_job_manager] = lambda: manager

    with TestClient(app) as client:
        missing_item_id = client.post(
            "/api/v1/jobs",
            json={
                "items": [{
                    "url": "https://www.toutiao.com/article/1/",
                    "media_name": "今日头条",
                    "type": "interactions",
                }]
            },
        )
        duplicate_item_id = client.post(
            "/api/v1/jobs",
            json={
                "items": [
                    {
                        "item_id": "same",
                        "url": "https://www.toutiao.com/article/1/",
                        "media_name": "今日头条",
                        "type": "interactions",
                    },
                    {
                        "item_id": "same",
                        "url": "https://www.toutiao.com/article/2/",
                        "media_name": "今日头条",
                        "type": "interactions",
                    },
                ]
            },
        )
        submitted = client.post(
            "/api/v1/jobs",
            json={
                "items": [
                    {
                        "item_id": "ok",
                        "url": "https://www.toutiao.com/article/1/",
                        "media_name": "今日头条",
                        "type": "interactions",
                    },
                    {
                        "item_id": "failed",
                        "url": "https://www.toutiao.com/article/failed/",
                        "media_name": "今日头条",
                        "type": "interactions",
                    },
                ]
            },
        )
        job_id = submitted.json()["job_id"]
        status = _wait_for_job(client, job_id)
        results = client.get(f"/api/v1/jobs/{job_id}/results").json()["data"]
        invalid_cursor = client.get(
            f"/api/v1/jobs/{job_id}/results",
            params={"cursor": "invalid"},
        )
        missing_job = client.get("/api/v1/jobs/job_missing")

    assert missing_item_id.status_code == 422
    assert duplicate_item_id.status_code == 422
    assert status["status"] == "completed"
    assert status["progress"]["success"] == 1
    assert status["progress"]["failed"] == 1
    failed = next(item for item in results if item["item_id"] == "failed")
    assert failed["status"] == "failed"
    assert failed["error"] == "测试失败"
    assert invalid_cursor.status_code == 422
    assert missing_job.status_code == 404
    asyncio.run(manager.aclose())


def test_async_job_rejects_more_than_configured_item_limit() -> None:
    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(retention_seconds=60, max_items=1)
        request = CreateJobRequest.model_validate(
            {
                "items": [
                    {
                        "item_id": f"item-{index}",
                        "url": f"https://www.toutiao.com/article/{index}/",
                        "media_name": "今日头条",
                        "type": "interactions",
                    }
                    for index in range(2)
                ]
            }
        )

        async def collector(item) -> JobItemResponse:
            raise AssertionError("collector must not run")

        try:
            await manager.submit(
                request,
                collector=collector,
                proxy_usage_scope=service.proxy_usage_scope,
            )
        except ValueError as exc:
            assert str(exc) == "单个异步任务最多允许 1 个采集项"
        else:
            raise AssertionError("oversized job was accepted")
        await manager.aclose()

    asyncio.run(scenario())


def test_async_job_replaces_oversized_result_with_small_failure() -> None:
    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(retention_seconds=60, result_max_bytes=1)
        request = CreateJobRequest.model_validate(
            {
                "items": [{
                    "item_id": "oversized",
                    "url": "https://www.toutiao.com/article/1/",
                    "media_name": "今日头条",
                    "type": "interactions",
                }]
            }
        )

        async def collector(item) -> JobItemResponse:
            return JobItemResponse(
                item_id=item.item_id,
                url=item.url,
                media_name=item.media_name,
                type=item.type,
                status="success",
                complete=True,
                result={"likes": 1},
            )

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
        )
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not finish")
        results = await manager.get_results(submitted.job_id, cursor=None, limit=100)

        assert status.progress.failed == 1
        assert results is not None
        assert results.data[0].status == "failed"
        assert results.data[0].error == "单条结果超过 1 字节存储上限"
        await manager.aclose()

    asyncio.run(scenario())


def test_async_job_idempotency_conflict_and_webhook() -> None:
    webhook_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        webhook_calls.append(request)
        return httpx.Response(204, request=request)

    app = create_app()
    service = FakeJobService()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = BatchJobManager(
        retention_seconds=60,
        webhook_allowed_hosts={"callback.example.com"},
        webhook_client=client,
    )
    app.dependency_overrides[get_engagement_service] = lambda: service
    app.dependency_overrides[get_job_manager] = lambda: manager
    first_payload = {
        "items": [{
            "item_id": "one",
            "url": "https://www.toutiao.com/article/1/",
            "media_name": "今日头条",
            "type": "interactions",
        }],
        "webhook_url": "https://callback.example.com/engagement",
    }
    second_payload = {
        "items": [{
            "item_id": "two",
            "url": "https://www.toutiao.com/article/2/",
            "media_name": "今日头条",
            "type": "interactions",
        }]
    }

    with TestClient(app) as api:
        submitted = api.post(
            "/api/v1/jobs",
            json=first_payload,
            headers={"Idempotency-Key": "same-key"},
        )
        conflict = api.post(
            "/api/v1/jobs",
            json=second_payload,
            headers={"Idempotency-Key": "same-key"},
        )
        job_id = submitted.json()["job_id"]
        for _ in range(100):
            status = api.get(f"/api/v1/jobs/{job_id}").json()
            if status["webhook_status"] == "sent":
                break
            time.sleep(0.01)

        blocked_webhook = api.post(
            "/api/v1/jobs",
            json={
                **second_payload,
                "webhook_url": "https://untrusted.example.com/callback",
            },
        )

    assert conflict.status_code == 409
    assert status["status"] == "completed"
    assert status["webhook_status"] == "sent"
    assert len(webhook_calls) == 1
    assert blocked_webhook.status_code == 422
    asyncio.run(manager.aclose())
    asyncio.run(client.aclose())


def test_completed_job_survives_manager_restart(tmp_path) -> None:
    db_path = tmp_path / "jobs.sqlite3"

    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(retention_seconds=60, db_path=db_path)
        request = CreateJobRequest.model_validate(
            {
                "items": [{
                    "item_id": "persisted-item",
                    "url": "https://www.toutiao.com/article/1/",
                    "media_name": "今日头条",
                    "type": "interactions",
                }]
            }
        )

        async def collector(item) -> JobItemResponse:
            return JobItemResponse(
                item_id=item.item_id,
                url=item.url,
                media_name="今日头条",
                type=item.type,
                status="success",
                complete=False,
                result={"likes": 7},
            )

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
            idempotency_key="persisted-key",
        )
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not finish")
        await manager.aclose()

        restored = BatchJobManager(retention_seconds=60, db_path=db_path)
        restored_status = await restored.get_status(submitted.job_id)
        restored_results = await restored.get_results(
            submitted.job_id,
            cursor=None,
            limit=100,
        )
        repeated = await restored.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
            idempotency_key="persisted-key",
        )

        assert restored_status is not None
        assert restored_status.status == "completed"
        assert restored_status.progress.completed == 1
        assert restored_status.progress.success == 1
        assert restored_results is not None
        assert restored_results.data[0].item_id == "persisted-item"
        assert repeated.job_id == submitted.job_id
        await restored.aclose()

    asyncio.run(scenario())


def test_in_progress_job_is_failed_after_manager_restart(tmp_path) -> None:
    db_path = tmp_path / "jobs.sqlite3"

    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(retention_seconds=60, db_path=db_path)
        request = CreateJobRequest.model_validate(
            {
                "items": [{
                    "item_id": "interrupted-item",
                    "url": "https://www.toutiao.com/article/1/",
                    "media_name": "今日头条",
                    "type": "interactions",
                }]
            }
        )
        blocked = asyncio.Event()

        async def collector(item) -> JobItemResponse:
            await blocked.wait()
            raise AssertionError("unreachable")

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
        )
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "running":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not start")
        await manager.aclose()

        restored = BatchJobManager(retention_seconds=60, db_path=db_path)
        restored_status = await restored.get_status(submitted.job_id)

        assert restored_status is not None
        assert restored_status.status == "failed"
        assert restored_status.finished_at is not None
        assert restored_status.cost_yuan == 0.00168
        await restored.aclose()

    asyncio.run(scenario())


def test_running_job_returns_cursor_for_later_results() -> None:
    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(retention_seconds=60)
        first_done = asyncio.Event()
        release_second = asyncio.Event()
        request = CreateJobRequest.model_validate(
            {
                "items": [
                    {
                        "item_id": "first",
                        "url": "https://www.toutiao.com/article/1/",
                        "media_name": "今日头条",
                        "type": "interactions",
                    },
                    {
                        "item_id": "second",
                        "url": "https://www.toutiao.com/article/2/",
                        "media_name": "今日头条",
                        "type": "interactions",
                    },
                ]
            }
        )

        async def collector(item) -> JobItemResponse:
            if item.item_id == "second":
                await release_second.wait()
            response = JobItemResponse(
                item_id=item.item_id,
                url=item.url,
                media_name="今日头条",
                type=item.type,
                status="success",
                complete=True,
                result={"likes": 1},
            )
            if item.item_id == "first":
                first_done.set()
            return response

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
        )
        await first_done.wait()
        for _ in range(100):
            first_page = await manager.get_results(
                submitted.job_id,
                cursor=None,
                limit=100,
            )
            if first_page is not None and first_page.available_count == 1:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("first result did not become available")

        assert first_page.next_cursor is not None
        release_second.set()
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not finish")
        second_page = await manager.get_results(
            submitted.job_id,
            cursor=first_page.next_cursor,
            limit=100,
        )

        assert second_page is not None
        assert [item.item_id for item in second_page.data] == ["second"]
        assert second_page.next_cursor is None
        await manager.aclose()

    asyncio.run(scenario())


def test_running_job_can_be_cancelled_without_marking_it_failed() -> None:
    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(retention_seconds=60, item_max_concurrency=1)
        started = asyncio.Event()
        never = asyncio.Event()
        request = CreateJobRequest.model_validate({
            "items": [{
                "item_id": "cancel-me",
                "url": "https://www.toutiao.com/article/1/",
                "media_name": "今日头条",
                "type": "interactions",
            }]
        })

        async def collector(item) -> JobItemResponse:
            started.set()
            await never.wait()
            raise AssertionError("unreachable")

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
        )
        await started.wait()
        cancelled = await manager.cancel(submitted.job_id)
        results = await manager.get_results(
            submitted.job_id,
            cursor=None,
            limit=100,
        )

        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.error == "任务已由调用方取消"
        assert cancelled.finished_at is not None
        assert results is not None
        assert results.status == "cancelled"
        assert results.available_count == 0
        assert results.next_cursor is None
        await manager.aclose()

    asyncio.run(scenario())


def test_job_item_timeout_isolated_as_a_failed_result() -> None:
    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(
            retention_seconds=60,
            item_timeout_seconds=0.01,
        )
        request = CreateJobRequest.model_validate({
            "items": [{
                "item_id": "slow",
                "url": "https://www.toutiao.com/article/1/",
                "media_name": "今日头条",
                "type": "interactions",
            }]
        })

        async def collector(item) -> JobItemResponse:
            await asyncio.sleep(1)
            raise AssertionError("unreachable")

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
        )
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not finish")
        results = await manager.get_results(submitted.job_id, cursor=None, limit=100)

        assert status.progress.failed == 1
        assert results is not None
        assert results.data[0].status == "failed"
        assert "超过 0.01 秒" in (results.data[0].error or "")
        assert results.data[0].duration_ms >= 0
        await manager.aclose()

    asyncio.run(scenario())


def test_zero_timeouts_disable_item_and_job_deadlines() -> None:
    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(
            retention_seconds=60,
            item_timeout_seconds=0,
            job_timeout_seconds=0,
        )
        request = CreateJobRequest.model_validate({
            "items": [{
                "item_id": "unbounded",
                "url": "https://www.toutiao.com/article/1/",
                "media_name": "今日头条",
                "type": "interactions",
            }]
        })

        async def collector(item) -> JobItemResponse:
            await asyncio.sleep(0.03)
            return JobItemResponse(
                item_id=item.item_id,
                url=item.url,
                media_name=item.media_name,
                type=item.type,
                status="success",
                complete=True,
                result={},
            )

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
        )
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not finish")

        assert status.progress.success == 1
        await manager.aclose()

    asyncio.run(scenario())


def test_job_circuit_backpressure_does_not_consume_item_timeout() -> None:
    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(
            retention_seconds=60,
            item_timeout_seconds=0.01,
        )
        request = CreateJobRequest.model_validate({
            "items": [{
                "item_id": "delayed-before-collect",
                "url": "https://www.douyin.com/video/1",
                "media_name": "抖音",
                "type": "interactions",
            }]
        })

        async def before_collect(item) -> None:
            await asyncio.sleep(0.03)

        async def collector(item) -> JobItemResponse:
            return JobItemResponse(
                item_id=item.item_id,
                url=item.url,
                media_name=item.media_name,
                type=item.type,
                status="success",
                complete=True,
                result={},
            )

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
            before_collect=before_collect,
        )
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not finish")
        results = await manager.get_results(
            submitted.job_id,
            cursor=None,
            limit=100,
        )

        assert status.progress.success == 1
        assert results is not None
        assert results.data[0].status == "success"
        assert results.data[0].duration_ms >= 30
        await manager.aclose()

    asyncio.run(scenario())


def test_job_timeout_cancels_remaining_workers() -> None:
    async def scenario() -> None:
        service = FakeJobService()
        manager = BatchJobManager(
            retention_seconds=60,
            item_timeout_seconds=1,
            job_timeout_seconds=0.02,
        )
        request = CreateJobRequest.model_validate({
            "items": [{
                "item_id": "slow",
                "url": "https://www.toutiao.com/article/1/",
                "media_name": "今日头条",
                "type": "interactions",
            }]
        })

        async def collector(item) -> JobItemResponse:
            await asyncio.sleep(1)
            raise AssertionError("unreachable")

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
        )
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "failed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not time out")

        assert status.error == "任务超过最大执行时间 0.02 秒"
        assert status.progress.completed == 0
        await manager.aclose()

    asyncio.run(scenario())


def test_running_progress_is_persisted_and_worker_count_is_bounded(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "jobs.sqlite3"
        service = FakeJobService()
        manager = BatchJobManager(
            retention_seconds=60,
            item_max_concurrency=2,
            db_path=db_path,
        )
        first_done = asyncio.Event()
        release = asyncio.Event()
        active = 0
        max_active = 0
        request = CreateJobRequest.model_validate({
            "items": [
                {
                    "item_id": f"item-{index}",
                    "url": f"https://www.toutiao.com/article/{index}/",
                    "media_name": "今日头条",
                    "type": "interactions",
                }
                for index in range(3)
            ]
        })

        async def collector(item) -> JobItemResponse:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                if item.item_id != "item-0":
                    await release.wait()
                return JobItemResponse(
                    item_id=item.item_id,
                    url=item.url,
                    media_name="今日头条",
                    type=item.type,
                    status="success",
                    complete=True,
                    result={"likes": 1},
                )
            finally:
                active -= 1
                if item.item_id == "item-0":
                    first_done.set()

        submitted = await manager.submit(
            request,
            collector=collector,
            proxy_usage_scope=service.proxy_usage_scope,
        )
        await first_done.wait()
        for _ in range(100):
            with sqlite3.connect(db_path) as connection:
                completed = connection.execute(
                    "SELECT completed FROM batch_jobs WHERE job_id = ?",
                    (submitted.job_id,),
                ).fetchone()[0]
            if completed == 1:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("running progress was not persisted")

        assert max_active == 2
        release.set()
        for _ in range(100):
            status = await manager.get_status(submitted.job_id)
            if status is not None and status.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("job did not finish")
        assert status.progress.completed == 3
        await manager.aclose()

    asyncio.run(scenario())
