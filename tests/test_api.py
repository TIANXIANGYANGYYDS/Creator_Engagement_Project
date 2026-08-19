from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_engagement_service
from app.models.engagement import EngagementResult


class FakeService:
    async def fetch(self, url: str, *, comment_limit: int) -> EngagementResult:
        return EngagementResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
            reason=f"limit={comment_limit}",
        )

    async def fetch_many(self, urls, *, comment_limit: int, concurrency: int):
        return [await self.fetch(url, comment_limit=comment_limit) for url in urls]


def test_health_and_engagement_routes() -> None:
    app = create_app()
    app.dependency_overrides[get_engagement_service] = lambda: FakeService()

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        result = client.get(
            "/api/v1/engagement",
            params={"url": "https://www.toutiao.com/article/123/", "comment_limit": 2},
        )

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert result.status_code == 200
    assert result.json()["reason"] == "limit=2"
