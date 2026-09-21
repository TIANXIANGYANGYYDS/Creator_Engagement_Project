from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.api.app import create_app
from app.api.dependencies import get_engagement_service, get_job_manager
from app.api import reliability
from app.api.routers import engagement
from app.models.engagement import InteractionResult
from app.services.engagement_service import EngagementService
from app.services.job_service import BatchJobManager


@pytest.mark.parametrize(
    "path", ["/api/v1/interactions", "/api/v1/comments", "/api/v1/collect"]
)
def test_sync_timeout_cancels_collection_and_releases_capacity(monkeypatch, path):
    monkeypatch.setattr(
        reliability,
        "get_settings",
        lambda: SimpleNamespace(api_sync_timeout_seconds=0.03),
    )

    class Crawler:
        def __init__(self):
            self.cancelled = False
            self.slow = True

        async def fetch_interactions(self, url, media_name):
            if self.slow:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled = True
            return InteractionResult(
                platform="toutiao",
                canonical_url=url,
                work_id="1",
                coverage="complete",
                stats={"likes": 1},
            )

        async def fetch_comments(self, url, media_name, page):
            return await self.fetch_interactions(url, media_name)

    async def run():
        crawler = Crawler()
        service = EngagementService(crawler, collection_max_concurrency=1)
        app = create_app()
        app.dependency_overrides[get_engagement_service] = lambda: service
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            item = {
                "url": "https://www.toutiao.com/article/1/",
                "media_name": "toutiao",
            }
            if path.endswith("collect"):
                response = await client.post(
                    path, json={"items": [{**item, "type": "interactions"}]}
                )
            else:
                response = await client.get(path, params=item)
            assert response.status_code == 504
            assert response.json()["request_id"] == response.headers["X-Request-ID"]
            assert crawler.cancelled
            assert not service._result_tasks
            assert not service._result_waiters
            crawler.slow = False
            response = await client.get("/api/v1/interactions", params=item)
            assert response.status_code == 200

    asyncio.run(run())


def test_queue_wait_is_included_in_sync_timeout(monkeypatch):
    monkeypatch.setattr(
        reliability,
        "get_settings",
        lambda: SimpleNamespace(api_sync_timeout_seconds=0.03),
    )

    async def run():
        service = EngagementService(SimpleNamespace(), collection_max_concurrency=1)
        app = create_app()
        app.dependency_overrides[get_engagement_service] = lambda: service
        async with service._collection_semaphore:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/v1/interactions",
                    params={
                        "url": "https://www.toutiao.com/article/1/",
                        "media_name": "toutiao",
                    },
                )
                assert response.status_code == 504
                assert not service._result_tasks

    asyncio.run(run())


def test_access_and_validation_logs_are_correlated_without_inputs(caplog):
    async def run():
        app = create_app()
        app.dependency_overrides[get_engagement_service] = lambda: SimpleNamespace()
        app.dependency_overrides[get_job_manager] = lambda: SimpleNamespace()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/jobs?token=secret-query",
                json={"items": [{"url": "secret-body"}]},
                headers={"Authorization": "secret-header"},
            )
            assert response.status_code == 422
            request_id = response.headers["X-Request-ID"]
            records = [r.message for r in caplog.records if request_id in r.message]
            assert any("request_started" in r for r in records)
            assert any("request_finished" in r and "status=422" in r for r in records)
            assert any(
                "request_validation_failed" in r and "item_id" in r for r in records
            )
            logged = "\n".join(
                r.message for r in caplog.records if r.name.startswith("app.")
            )
            assert all(
                secret not in logged
                for secret in ["secret-query", "secret-body", "secret-header"]
            )

    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(run())


def test_sync_limit_and_async_rejection_logs(monkeypatch, caplog):
    monkeypatch.setattr(
        engagement, "get_settings", lambda: SimpleNamespace(api_sync_max_items=1)
    )

    class Service:
        @asynccontextmanager
        async def proxy_usage_scope(self):
            yield SimpleNamespace(added_endpoint_count=0)

    async def run():
        app = create_app()
        manager = BatchJobManager()
        app.dependency_overrides[get_engagement_service] = lambda: Service()
        app.dependency_overrides[get_job_manager] = lambda: manager
        item = {
            "item_id": "duplicate",
            "url": "https://www.toutiao.com/article/1/",
            "media_name": "toutiao",
            "type": "interactions",
        }
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/collect", json={"items": [item, item]}
                )
                assert response.status_code == 422
                assert "/api/v1/jobs" in response.json()["detail"]
                response = await client.post(
                    "/api/v1/jobs", json={"items": [item, item]}
                )
                assert response.status_code == 422
                assert any(
                    "job_submission_rejected" in r.message
                    and response.headers["X-Request-ID"] in r.message
                    and "item_id" in r.message
                    for r in caplog.records
                )
        finally:
            await manager.aclose()

    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(run())


def test_unexpected_error_keeps_request_id(caplog):
    class Manager:
        async def get_status(self, job_id):
            raise RuntimeError("simulated database failure")

    async def run():
        app = create_app()
        app.dependency_overrides[get_job_manager] = lambda: Manager()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/v1/jobs/job_test")
            assert response.status_code == 500
            assert response.json() == {"detail": "服务器内部错误"}
            request_id = response.headers["X-Request-ID"]
            assert any(
                "request_finished" in r.message
                and request_id in r.message
                and "status=500" in r.message
                for r in caplog.records
            )

    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(run())


def test_sync_default_does_not_reject_5925_items(monkeypatch):
    from app.core.config import Settings
    from app.models.engagement import InteractionResult

    monkeypatch.setattr(engagement, 'get_settings', lambda: Settings(_env_file=None))

    class Service:
        @asynccontextmanager
        async def proxy_usage_scope(self):
            yield SimpleNamespace(added_endpoint_count=0)

        async def fetch_interactions(self, url, media_name):
            return InteractionResult(platform='toutiao', canonical_url=url, work_id='1', coverage='complete', stats={'likes': 1})

    async def run():
        app = create_app()
        app.dependency_overrides[get_engagement_service] = lambda: Service()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            item = {'url': 'https://www.toutiao.com/article/1/', 'media_name': 'toutiao', 'type': 'interactions'}
            response = await client.post('/api/v1/collect', json={'items': [item] * 5925})
            assert response.status_code == 200
            assert len(response.json()['data']) == 5925
    asyncio.run(run())
