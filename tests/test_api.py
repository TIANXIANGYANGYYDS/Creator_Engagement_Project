from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_engagement_service
from app.models.engagement import CommentPageResult, EngagementComment, InteractionResult


class FakeService:
    async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
        if media_name == "微博":
            raise ValueError("media_name does not match URL platform")
        return InteractionResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
            reason=f"media={media_name}",
            stats={"comments": 28},
        )

    async def fetch_comments(self, url: str, media_name: str, page: int) -> CommentPageResult:
        return CommentPageResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
            page=page,
            comments=[EngagementComment(comment_id="c1", text=media_name)],
            next_page=page + 1,
            total_comments=28,
        )

def test_health_interactions_and_comments_routes() -> None:
    app = create_app()
    app.dependency_overrides[get_engagement_service] = lambda: FakeService()

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        interactions = client.get(
            "/api/v1/interactions",
            params={"url": "https://www.toutiao.com/article/123/", "media_name": "头条"},
        )
        comments = client.get(
            "/api/v1/comments",
            params={
                "url": "https://www.toutiao.com/article/123/",
                "media_name": "toutiao",
                "page": 2,
            },
        )

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert interactions.status_code == 200
    assert interactions.json()["reason"] == "media=头条"
    assert "comments" not in interactions.json()
    assert comments.status_code == 200
    assert comments.json()["page"] == 2
    assert comments.json()["comments"][0]["text"] == "toutiao"
    assert "stats" not in comments.json()


def test_api_rejects_invalid_page_and_media_mismatch() -> None:
    app = create_app()
    app.dependency_overrides[get_engagement_service] = lambda: FakeService()

    with TestClient(app) as client:
        invalid_page = client.get(
            "/api/v1/comments",
            params={"url": "https://m.weibo.cn/detail/12345678", "media_name": "微博", "page": 0},
        )
        mismatch = client.get(
            "/api/v1/interactions",
            params={"url": "https://www.toutiao.com/article/123/", "media_name": "微博"},
        )

    assert invalid_page.status_code == 422
    assert mismatch.status_code == 422
    assert "does not match" in mismatch.json()["detail"]
