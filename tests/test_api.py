from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_engagement_service
from app.models.engagement import CommentPageResult, EngagementComment, InteractionResult


class FakeService:
    def __init__(self) -> None:
        self.comment_calls: list[tuple[int, str | None]] = []
        self.added_proxy_ips = 0

    @asynccontextmanager
    async def proxy_usage_scope(self):
        yield SimpleNamespace(added_endpoint_count=self.added_proxy_ips)

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

    async def fetch_comments(
        self,
        url: str,
        media_name: str,
        page: int,
        *,
        cursor: str | None = None,
    ) -> CommentPageResult:
        self.comment_calls.append((page, cursor))
        return CommentPageResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
            page=page,
            comments=[EngagementComment(comment_id=f"c{page}", text=media_name)],
            next_page=page + 1 if page < 2 else None,
            next_cursor="cursor-2" if page < 2 else None,
            total_comments=2,
            capabilities={
                "root_comments": "all_public_pages",
                "anonymous": True,
            },
        )


class CanonicalNameService(FakeService):
    async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
        platform = "wechat" if media_name == "微信公众号" else "bilibili"
        return InteractionResult(
            platform=platform,
            canonical_url=url,
            work_id="123",
            coverage="complete",
            stats={"likes": 1},
        )


class IncompleteCommentService(FakeService):
    async def fetch_comments(
        self,
        url: str,
        media_name: str,
        page: int,
        *,
        cursor: str | None = None,
    ) -> CommentPageResult:
        return CommentPageResult(
            platform="xiaohongshu",
            canonical_url=url,
            work_id="xhs-note",
            coverage="partial",
            page=page,
            comments=[EngagementComment(comment_id="xhs-1", text="游客首批")],
            total_comments=100,
            capabilities={
                "root_comments": "first_public_page",
                "anonymous": True,
            },
        )


def test_health_interactions_and_comments_routes() -> None:
    app = create_app()
    service = FakeService()
    app.dependency_overrides[get_engagement_service] = lambda: service

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
        all_comments = client.get(
            "/api/v1/comments",
            params={
                "url": "https://www.toutiao.com/article/123/",
                "media_name": "toutiao",
            },
        )

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["collection_max_concurrency"] == 8
    assert health.json()["browser_max_concurrency"] == 3
    assert health.json()["browser_max_attempts"] == 3
    assert health.json()["douyin_protocol_max_attempts"] == 2
    assert health.json()["job_item_timeout_seconds"] == 45
    assert health.json()["job_timeout_seconds"] == 1800
    assert interactions.status_code == 200
    assert interactions.json()["media_name"] == "今日头条"
    assert interactions.json()["data"]["comments"] == 28
    assert set(interactions.json()) == {"media_name", "data"}
    assert comments.status_code == 200
    assert comments.json()["media_name"] == "今日头条"
    assert comments.json()["comments"][0]["comment_id"] == "c2"
    assert comments.json()["comments"][0]["text"] == "今日头条"
    assert set(comments.json()) == {"media_name", "comments"}
    assert [item["comment_id"] for item in all_comments.json()["comments"]] == ["c1", "c2"]
    assert service.comment_calls == [(2, None), (1, None), (2, "cursor-2")]


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


def test_collect_returns_uniform_results_chinese_media_and_batch_cost() -> None:
    app = create_app()
    service = FakeService()
    service.added_proxy_ips = 2
    app.dependency_overrides[get_engagement_service] = lambda: service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/collect",
            json={
                "items": [
                    {
                        "url": "https://www.toutiao.com/article/123/",
                        "media_name": "头条",
                        "type": "interactions",
                    },
                    {
                        "url": "https://www.toutiao.com/article/456/",
                        "media_name": "今日头条",
                        "type": "comments",
                    },
                ]
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["cost_yuan"] == 0.00168
    assert payload["duration_ms"] >= 0
    assert [item["media_name"] for item in payload["data"]] == [
        "今日头条",
        "今日头条",
    ]
    assert [item["status"] for item in payload["data"]] == ["success", "success"]
    assert [item["complete"] for item in payload["data"]] == [True, True]
    result_keys = {
        "views",
        "likes",
        "total_comments",
        "shares",
        "favorites",
        "coins",
        "danmaku",
        "reposts",
        "recommendations",
        "comments",
    }
    assert set(payload["data"][0]["result"]) == result_keys
    assert set(payload["data"][1]["result"]) == result_keys
    assert payload["data"][0]["result"]["total_comments"] == 28
    assert payload["data"][0]["result"]["comments"] is None
    assert payload["data"][1]["result"]["views"] is None
    assert [
        comment["comment_id"] for comment in payload["data"][1]["result"]["comments"]
    ] == ["c1", "c2"]
    assert service.comment_calls == [(1, None), (2, "cursor-2")]


def test_collect_has_no_item_count_limit_and_isolates_item_failures() -> None:
    app = create_app()
    service = FakeService()
    app.dependency_overrides[get_engagement_service] = lambda: service
    items = [
        {
            "url": f"https://www.toutiao.com/article/{index}/",
            "media_name": "今日头条",
            "type": "interactions",
        }
        for index in range(101)
    ]
    items.append({
        "url": "https://www.toutiao.com/article/999/",
        "media_name": "微博",
        "type": "interactions",
    })

    with TestClient(app) as client:
        response = client.post("/api/v1/collect", json={"items": items})
        invalid_type = client.post(
            "/api/v1/collect",
            json={
                "items": [{
                    "url": "https://www.toutiao.com/article/123/",
                    "media_name": "今日头条",
                    "type": "all",
                }]
            },
        )

    assert response.status_code == 200
    assert len(response.json()["data"]) == 102
    failed = response.json()["data"][-1]
    assert failed["status"] == "failed"
    assert failed["complete"] is False
    assert failed["media_name"] == "微博"
    assert failed["error"] == "media_name does not match URL platform"
    assert all(value is None for value in failed["result"].values())
    assert invalid_type.status_code == 422


def test_collect_returns_canonical_wechat_and_bilibili_names() -> None:
    app = create_app()
    app.dependency_overrides[get_engagement_service] = lambda: CanonicalNameService()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/collect",
            json={
                "items": [
                    {
                        "url": "https://mp.weixin.qq.com/s/article",
                        "media_name": "微信公众号",
                        "type": "interactions",
                    },
                    {
                        "url": "https://www.bilibili.com/video/BVxxx",
                        "media_name": "B站",
                        "type": "interactions",
                    },
                    {
                        "url": "https://www.bilibili.com/video/BVyyy",
                        "media_name": "哔哩哔哩",
                        "type": "interactions",
                    },
                ]
            },
        )

    assert response.status_code == 200
    assert [item["media_name"] for item in response.json()["data"]] == [
        "微信公众号",
        "哔哩哔哩",
        "哔哩哔哩",
    ]


def test_collect_comments_without_page_fetches_all_pages() -> None:
    app = create_app()
    service = FakeService()
    app.dependency_overrides[get_engagement_service] = lambda: service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/collect",
            json={
                "items": [{
                    "url": "https://www.toutiao.com/article/123/",
                    "media_name": "今日头条",
                    "type": "comments",
                }]
            },
        )

    assert response.status_code == 200
    assert service.comment_calls == [(1, None), (2, "cursor-2")]
    assert len(response.json()["data"][0]["result"]["comments"]) == 2
    assert response.json()["data"][0]["complete"] is True


def test_collect_distinguishes_success_from_incomplete_coverage() -> None:
    app = create_app()
    app.dependency_overrides[get_engagement_service] = lambda: IncompleteCommentService()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/collect",
            json={
                "items": [{
                    "url": "https://www.xiaohongshu.com/explore/xhs-note",
                    "media_name": "小红书",
                    "type": "comments",
                }]
            },
        )

    item = response.json()["data"][0]
    assert item["status"] == "success"
    assert item["complete"] is False
    assert item["result"]["comments"][0]["comment_id"] == "xhs-1"


def test_collect_comments_with_numeric_page_fetches_only_that_page() -> None:
    app = create_app()
    service = FakeService()
    app.dependency_overrides[get_engagement_service] = lambda: service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/collect",
            json={
                "items": [{
                    "url": "https://www.toutiao.com/article/123/",
                    "media_name": "今日头条",
                    "type": "comments",
                    "page": 2,
                }]
            },
        )

    assert response.status_code == 200
    assert service.comment_calls == [(2, None)]
    assert [
        comment["comment_id"] for comment in response.json()["data"][0]["result"]["comments"]
    ] == ["c2"]


def test_collect_comment_page_must_be_a_json_number() -> None:
    app = create_app()
    app.dependency_overrides[get_engagement_service] = lambda: FakeService()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/collect",
            json={
                "items": [{
                    "url": "https://www.toutiao.com/article/123/",
                    "media_name": "今日头条",
                    "type": "comments",
                    "page": "2",
                }]
            },
        )

    assert response.status_code == 422
