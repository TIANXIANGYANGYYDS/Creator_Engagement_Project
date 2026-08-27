from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.wechat_channels_bridge import HttpWeChatChannelsBridgeClient
from tests.test_engagement_crawler import FakeClient, FakeResponse


PUBLIC_PREVIEW = FakeResponse(payload={
    "errCode": 0,
    "data": {
        "feedInfo": {
            "description": "目标视频",
            "commentCountFmt": "128",
        },
        "errMsg": {"type": 0},
    },
})
MOBILE_FEED_ID = "export/UzFfBgAAxN6jAAkGAmPvk8zT4DCJorvgXiwL15tbF2yqxVCjFw"
MOBILE_FEED_URL = (
    "https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?"
    "api=openFinderView&extInfo=%7B%22action%22%3A%22openFinderFeed%22%2C"
    "%22feedID%22%3A%22export%2FUzFfBgAAxN6jAAkGAmPvk8zT4DCJorvgXiwL15tbF2yqxVCjFw%22%7D"
)


def test_channels_bridge_rejects_unprotected_remote_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpWeChatChannelsBridgeClient("http://192.0.2.10:2026", "secret")

    with pytest.raises(ValueError, match="token"):
        HttpWeChatChannelsBridgeClient("https://channels.example.com")


class FakeChannelsBridge:
    async def fetch_comments(
        self,
        url: str,
        *,
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        assert url == "https://weixin.qq.com/sph/AoPX5bEBDd"
        assert page == 1
        assert limit == 20
        return {
            "comments": [{
                "commentId": "comment-1",
                "nickname": "读者",
                "content": "视频号评论正文",
                "createtime": "1776442446",
                "likeCount": 13,
                "expandCommentCount": 2,
            }],
            "total_comments": 128,
            "next_marker": "opaque-buffer",
            "source": "wx_channel/finderGetCommentList",
        }


class UnavailableChannelsBridge:
    async def fetch_comments(
        self,
        url: str,
        *,
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        raise PlatformCrawlerError("没有可用的微信页面")


class MobileChannelsBridge:
    async def fetch_interactions(self, url: str) -> dict[str, Any]:
        assert url == MOBILE_FEED_URL
        return {
            "stats": {
                "views": 8765,
                "likes": 432,
                "comments": 21,
                "shares": 18,
                "favorites": 76,
            },
            "source": "wx_channel/finderGetCommentDetail",
        }


def test_wechat_channels_comments_use_authorized_sidecar() -> None:
    client = FakeClient(PUBLIC_PREVIEW)
    result = asyncio.run(EngagementCrawler(
        client=client,
        wechat_channels_bridge_client=FakeChannelsBridge(),
    ).fetch_comments(
        "https://weixin.qq.com/sph/AoPX5bEBDd",
        "wechat_channels",
        1,
    ))

    assert result.coverage == "partial"
    assert result.total_comments == 128
    assert result.next_page == 2
    assert result.comments[0].comment_id == "comment-1"
    assert result.comments[0].text == "视频号评论正文"
    assert result.comments[0].likes == 13
    assert result.comments[0].replies == 2
    assert result.source == "wx_channel/finderGetCommentList"
    assert client.calls == []


def test_wechat_channels_sidecar_failure_falls_back_to_public_count() -> None:
    client = FakeClient(PUBLIC_PREVIEW)
    result = asyncio.run(EngagementCrawler(
        client=client,
        wechat_channels_bridge_client=UnavailableChannelsBridge(),
    ).fetch_comments(
        "https://weixin.qq.com/sph/AoPX5bEBDd",
        "wechat_channels",
        1,
    ))

    assert result.coverage == "unsupported"
    assert result.total_comments == 128
    assert result.comments == []
    assert "没有可用的微信页面" in result.reason
    assert len(client.calls) == 1


def test_mobile_wechat_channels_interactions_use_authorized_sidecar() -> None:
    client = FakeClient(FakeResponse(payload={}))
    result = asyncio.run(EngagementCrawler(
        client=client,
        wechat_channels_bridge_client=MobileChannelsBridge(),
    ).fetch_interactions(
        MOBILE_FEED_URL,
        "微信视频号",
    ))

    assert result.coverage == "partial"
    assert result.work_id == MOBILE_FEED_ID
    assert result.stats.views == 8765
    assert result.stats.likes == 432
    assert result.stats.comments == 21
    assert result.source == "wx_channel/finderGetCommentDetail"
    assert client.calls == []


def test_mobile_wechat_channels_without_sidecar_does_not_fake_public_support() -> None:
    client = FakeClient(FakeResponse(payload={}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        MOBILE_FEED_URL,
        "微信视频号",
    ))

    assert result.coverage == "unsupported"
    assert result.stats.model_dump() == {
        "views": None,
        "likes": None,
        "comments": None,
        "shares": None,
        "favorites": None,
        "coins": None,
        "danmaku": None,
        "reposts": None,
        "recommendations": None,
    }
    assert "encrypted_object_id" in result.reason
    assert client.calls == []


def test_channels_bridge_resolves_profile_and_follows_opaque_cursor() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/channels/feed/profile":
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "errCode": 0,
                    "data": {
                        "object": {
                            "id": "object-1",
                            "objectNonceId": "nonce-1",
                        }
                    },
                },
            })
        marker = request.url.params.get("next_marker")
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "errCode": 0,
                "data": {
                    "commentInfo": [{
                        "commentId": "page-2" if marker else "page-1",
                        "content": "第二页" if marker else "第一页",
                    }],
                    "countInfo": {"commentCount": 2},
                    "lastBuffer": "" if marker else "opaque-next",
                },
            },
        })

    async def scenario() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            bridge = HttpWeChatChannelsBridgeClient(
                "http://127.0.0.1:2026",
                "local-secret",
                client=client,
            )
            return await bridge.fetch_comments(
                "https://weixin.qq.com/sph/AoPX5bEBDd",
                page=2,
                limit=20,
            )

    result = asyncio.run(scenario())

    assert [item["commentId"] for item in result["comments"]] == ["page-2"]
    assert result["total_comments"] == 2
    assert result["next_marker"] == ""
    assert result["exhausted"] is True
    assert len(calls) == 3
    assert calls[1].url.params.get("next_marker") == ""
    assert calls[2].url.params.get("next_marker") == "opaque-next"
    assert all(call.headers["X-Local-Auth"] == "local-secret" for call in calls)


def test_channels_bridge_uses_mobile_encrypted_feed_id_for_profile() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/channels/feed/profile":
            assert request.url.params["encrypted_object_id"] == MOBILE_FEED_ID
            assert "url" not in request.url.params
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "errCode": 0,
                    "data": {
                        "object": {
                            "id": "object-mobile",
                            "objectNonceId": "nonce-mobile",
                            "readCount": 8765,
                            "likeCount": 432,
                            "commentCount": 21,
                            "forwardCount": 18,
                            "favCount": 76,
                        }
                    },
                },
            })
        assert request.url.path == "/api/channels/feed/comment/list"
        assert request.url.params["object_id"] == "object-mobile"
        assert request.url.params["nonce_id"] == "nonce-mobile"
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "errCode": 0,
                "data": {
                    "commentInfo": [{"commentId": "mobile-comment"}],
                    "countInfo": {"commentCount": 21},
                    "lastBuffer": "",
                },
            },
        })

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            bridge = HttpWeChatChannelsBridgeClient(
                "http://127.0.0.1:2026",
                client=client,
            )
            interactions = await bridge.fetch_interactions(MOBILE_FEED_URL)
            comments = await bridge.fetch_comments(
                MOBILE_FEED_URL,
                page=1,
                limit=20,
            )
            return interactions, comments

    interactions, comments = asyncio.run(scenario())

    assert interactions["stats"] == {
        "views": 8765,
        "likes": 432,
        "comments": 21,
        "shares": 18,
        "favorites": 76,
    }
    assert comments["comments"][0]["commentId"] == "mobile-comment"
    assert len(calls) == 3


def test_channels_bridge_uses_exact_title_search_when_share_profile_lacks_nonce() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/channels/feed/profile":
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "errCode": 0,
                    "data": {
                        "feedInfo": {"description": "完全一致的视频标题"},
                        "authorInfo": {"nickname": "目标作者"},
                        "object": {"id": "export/not-a-comment-id"},
                    },
                },
            })
        if request.url.path == "/api/channels/feed/search":
            assert request.url.params["keyword"] == "完全一致的视频标题"
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "errCode": 0,
                    "data": {
                        "objectList": [{
                            "id": "object-2",
                            "objectNonceId": "nonce-2",
                            "contact": {"nickname": "目标作者"},
                            "objectDesc": {"description": "完全一致的视频标题"},
                        }]
                    },
                },
            })
        assert request.url.params["object_id"] == "object-2"
        assert request.url.params["nonce_id"] == "nonce-2"
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "errCode": 0,
                "data": {
                    "commentInfo": [{"commentId": "matched", "content": "命中"}],
                    "countInfo": {"commentCount": 1},
                    "lastBuffer": "",
                },
            },
        })

    async def scenario() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            bridge = HttpWeChatChannelsBridgeClient(
                "http://127.0.0.1:2026",
                client=client,
            )
            return await bridge.fetch_comments(
                "https://weixin.qq.com/sph/AoPX5bEBDd",
                page=1,
                limit=20,
            )

    result = asyncio.run(scenario())

    assert result["comments"][0]["commentId"] == "matched"
    assert paths == [
        "/api/channels/feed/profile",
        "/api/channels/feed/search",
        "/api/channels/feed/comment/list",
    ]
