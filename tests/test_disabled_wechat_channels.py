from __future__ import annotations

import asyncio
from time import monotonic

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_engagement_service
from app.models.engagement import CommentPageResult, InteractionResult
from app.services.engagement_service import EngagementService, PlatformTrafficPolicy


class NoCollection:
    def __getattr__(self, name):
        raise AssertionError(f"disabled channel must not access collector or proxy: {name}")


CHANNELS = [
    ("wechat", "公众号", "http://mp.weixin.qq.com/s/article"),
    ("wechat", "微信公众号", "https://mp.weixin.qq.com/s/article"),
    ("wechat", "weixin", "https://mp.weixin.qq.com/s/article"),
    ("wechat_channels", "视频号", "https://weixin.qq.com/sph/abc123"),
    ("wechat_channels", "微信视频号", "https://channels.weixin.qq.com/finder-preview/pages/sph?id=abc123"),
    ("wechat_channels", "channels", "https://weixin.qq.com/sph/abc123"),
]


@pytest.mark.parametrize("platform,media,url", CHANNELS)
def test_disabled_channels_skip_cache_capacity_and_all_collection(platform, media, url):
    service = EngagementService(
        NoCollection(),
        proxy_provider=NoCollection(),
        platform_policies={platform: PlatformTrafficPolicy(1, 60)},
    )
    # A previous success must not override the current channel policy.
    service._result_cache[("interactions", platform, url, 0, None)] = (
        monotonic() + 60, 0,
        InteractionResult(platform=platform, canonical_url=url, work_id="old",
                          coverage="complete", stats={"likes": 999}),
    )
    service._result_cache[("comments", platform, url, 1, None)] = (
        monotonic() + 60, 0,
        CommentPageResult(platform=platform, canonical_url=url, work_id="old",
                          coverage="complete", page=1, total_comments=0),
    )

    async def run():
        service._collection_semaphore = asyncio.Semaphore(0)
        service._platform_semaphores[platform] = asyncio.Semaphore(0)
        service._platform_rate_limiters[(platform, "interactions")] = NoCollection()
        service._platform_rate_limiters[(platform, "comments")] = NoCollection()
        await service.wait_for_platform_ready(media)
        async with service.collection_slot(media, "interactions"):
            interactions = await service.fetch_interactions(url, media)
        async with service.collection_slot(media, "comments"):
            comments = await service.fetch_comments(url, media, 1)
        later_page = await service.fetch_comments(url, media, 2, cursor="old-cursor")
        combined = await service.fetch(url)
        for result in (interactions, comments, later_page, combined):
            assert result.platform == platform
            assert result.canonical_url == url
            assert result.coverage == "unsupported"
            assert "已停用" in result.reason
            assert result.source == "channel_policy"
            assert result.protocol_attempts == 0
        assert all(v is None for v in interactions.stats.model_dump().values())
        assert all(v is None for v in combined.stats.model_dump().values())
        for result in (comments, later_page):
            assert result.total_comments is None
            assert result.comments == []
            assert result.next_page is None
            assert result.next_cursor is None
            assert result.comments_complete is False
            assert result.capabilities.root_comments == "unavailable"

    async def bounded():
        await asyncio.wait_for(run(), timeout=1)

    asyncio.run(bounded())


def test_disabled_channel_keeps_input_validation():
    service = EngagementService(NoCollection())

    async def run():
        with pytest.raises(ValueError, match="does not match"):
            await service.fetch_interactions("https://www.bilibili.com/video/BVabc", "微信")
        with pytest.raises(ValueError, match="page"):
            await service.fetch_comments("https://mp.weixin.qq.com/s/article", "微信", 0)
        with pytest.raises(ValueError, match="comment_limit"):
            await service.fetch("https://mp.weixin.qq.com/s/article", comment_limit=0)

    asyncio.run(run())


def test_disabled_channels_fail_single_and_batch_api_without_collection():
    service = EngagementService(NoCollection(), proxy_provider=NoCollection())
    # Proxy usage accounting is local bookkeeping, not collection.
    service.proxy_provider = None
    app = create_app()
    app.dependency_overrides[get_engagement_service] = lambda: service
    items = []
    with TestClient(app) as client:
        for _, media, url in (CHANNELS[0], CHANNELS[3]):
            for operation in ("interactions", "comments"):
                params = {"url": url, "media_name": media}
                response = client.get(f"/api/v1/{operation}", params=params)
                assert response.status_code == 502
                assert "已停用" in response.json()["detail"]
                items.append({**params, "type": operation})
            response = client.get("/api/v1/comments", params={**params, "page": 2})
            assert response.status_code == 502
            items.append({**params, "type": "comments", "page": 2})
        response = client.post("/api/v1/collect", json={"items": items})
    assert response.status_code == 200
    results = response.json()["data"]
    assert len(results) == len(items)
    for result in results:
        assert result["status"] == "failed"
        assert result["complete"] is False
        assert "已停用" in result["error"]
        assert all(v is None for v in result["result"].values())
