from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.wechat_session_bridge import (
    BridgeCaptureRequest,
    BridgeFetchRequest,
    HttpWeChatSessionBridgeClient,
    WeChatSessionBridge,
    WeChatSessionStore,
    create_wechat_bridge_app,
)


SECRET_VALUES = (
    "key-secret",
    "ticket-secret",
    "token-secret",
    "cookie-secret",
)


def capture_request() -> BridgeCaptureRequest:
    return BridgeCaptureRequest(
        request_url=(
            "https://mp.weixin.qq.com/s/demo?__biz=MzDemo&mid=20&idx=1"
            "&uin=12&key=key-secret&pass_ticket=ticket-secret"
        ),
        request_headers={
            "Cookie": "wap_sid2=cookie-secret",
            "User-Agent": "MicroMessenger/Test",
        },
        response_body=(
            "<script>var appmsg_token='token-secret';"
            "window.cgiDataNew={bizuin:'MzDemo',mid:20,idx:1};</script>"
        ),
    )


def test_wechat_session_store_status_is_redacted_and_memory_only() -> None:
    store = WeChatSessionStore(ttl_seconds=60)
    captured, biz = store.capture(capture_request())

    assert captured is True
    assert biz == "MzDemo"
    status = store.status()
    assert status["sessions"][0]["status"] == "valid"
    assert status["sessions"][0]["biz"] == "MzDemo"
    for secret in SECRET_VALUES:
        assert secret not in str(status)


def test_wechat_bridge_interactions_replay_the_captured_session() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=(
                "var appmsg_read_num=1234;var appmsg_like_num=56;"
                "var share_count=7;var comment_count=8;"
            ),
            request=request,
        )

    async def scenario() -> dict[str, Any]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        bridge = WeChatSessionBridge(client=client)
        bridge.capture(capture_request())
        try:
            return await bridge.interactions(BridgeFetchRequest(
                url="https://mp.weixin.qq.com/s/demo",
                metadata={"biz": "MzDemo", "mid": "20", "idx": "1"},
            ))
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert result["ok"] is True
    assert result["stats"] == {
        "views": 1234,
        "likes": 56,
        "comments": 8,
        "shares": 7,
        "favorites": None,
        "coins": None,
        "danmaku": None,
        "reposts": None,
        "recommendations": None,
    }
    assert len(requests) == 1
    assert requests[0].url.params["appmsg_token"] == "token-secret"
    assert requests[0].headers["cookie"] == "wap_sid2=cookie-secret"
    for secret in SECRET_VALUES:
        assert secret not in str(result)


def test_wechat_bridge_falls_back_to_getappmsgext_for_missing_counters() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/mp/getappmsgext":
            return httpx.Response(
                200,
                json={
                    "base_resp": {"ret": 0},
                    "appmsgstat": {"read_num": 88, "like_num": 6, "share_count": 3},
                },
                request=request,
            )
        return httpx.Response(
            200,
            text="var comment_count=2;",
            request=request,
        )

    async def scenario() -> dict[str, Any]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        bridge = WeChatSessionBridge(client=client)
        bridge.capture(capture_request())
        try:
            return await bridge.interactions(BridgeFetchRequest(
                url="https://mp.weixin.qq.com/s/demo",
                metadata={
                    "biz": "MzDemo",
                    "mid": "20",
                    "idx": "1",
                    "comment_id": "comment-20",
                },
            ))
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert [request.url.path for request in requests] == ["/s/demo", "/mp/getappmsgext"]
    assert result["stats"]["views"] == 88
    assert result["stats"]["likes"] == 6
    assert result["stats"]["comments"] == 2
    assert result["source"].endswith("mp/getappmsgext")


def test_wechat_bridge_comments_translate_numbered_page_to_elected_rows() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mp/appmsg_comment"
        assert request.url.params["appmsgid"] == "20"
        rows = [
            {
                "id": f"c{index}",
                "nick_name": f"读者{index}",
                "content": f"评论{index}",
                "like_num": index,
            }
            for index in range(25)
        ]
        return httpx.Response(
            200,
            json={
                "base_resp": {"ret": 0},
                "elected_comment": rows,
                "total_count": 25,
                "continue_flag": 0,
            },
            request=request,
        )

    async def scenario() -> dict[str, Any]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        bridge = WeChatSessionBridge(client=client)
        bridge.capture(capture_request())
        try:
            return await bridge.comments(BridgeFetchRequest(
                url="https://mp.weixin.qq.com/s/demo",
                metadata={
                    "biz": "MzDemo",
                    "mid": "20",
                    "idx": "1",
                    "comment_id": "comment-20",
                },
                page=2,
                limit=20,
            ))
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert result["ok"] is True
    assert len(result["comments"]) == 5
    assert result["comments"][0]["comment_id"] == "c20"
    assert result["total_comments"] == 25
    assert result["has_more"] is False
    assert result["comment_scope"] == "elected"


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text
        self.url = "https://mp.weixin.qq.com/s/demo"
        self.status_code = 200


class _ArticleClient:
    async def get(self, _url: str, **_kwargs: Any) -> _Response:
        return _Response(
            "<script>window.cgiDataNew={show_comment:1,comment_id:10,"
            "bizuin:'MzDemo',mid:20,idx:1};</script>"
        )


class _MainBridgeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def fetch(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((operation, kwargs["page"]))
        if operation == "interactions":
            return {
                "ok": True,
                "source": "wechat_client_observed_response",
                "stats": {"views": 321, "likes": 9, "comments": 1},
            }
        return {
            "ok": True,
            "source": "mp/appmsg_comment",
            "comments": [{"comment_id": "c1", "author": "读者", "text": "有效"}],
            "total_comments": 1,
            "has_more": False,
        }


def test_main_wechat_collector_prefers_nonofficial_session_bridge() -> None:
    bridge = _MainBridgeClient()
    crawler = EngagementCrawler(
        client=_ArticleClient(),
        wechat_session_bridge_client=bridge,
    )

    interactions = asyncio.run(crawler.fetch_interactions(
        "https://mp.weixin.qq.com/s/demo", "公众号",
    ))
    comments = asyncio.run(crawler.fetch_comments(
        "https://mp.weixin.qq.com/s/demo", "公众号", 1,
    ))

    assert interactions.stats.views == 321
    assert interactions.source == "wechat_client_observed_response"
    assert comments.comments[0].text == "有效"
    assert comments.source == "mp/appmsg_comment"
    assert comments.next_page is None
    assert bridge.calls == [("interactions", 1), ("comments", 1)]


def test_bridge_configuration_rejects_remote_plain_http_and_short_token() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpWeChatSessionBridgeClient("http://192.0.2.1:8210", "x" * 24)
    with pytest.raises(ValueError, match="至少需要 24"):
        create_wechat_bridge_app("short")


def test_bridge_rejects_non_wechat_target_before_sending_credentials() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="unexpected", request=request)

    async def scenario() -> dict[str, Any]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        bridge = WeChatSessionBridge(client=client)
        bridge.capture(capture_request())
        try:
            return await bridge.interactions(BridgeFetchRequest(
                url="https://example.com/collect",
                metadata={"biz": "MzDemo", "mid": "20", "idx": "1"},
            ))
        finally:
            await client.aclose()

    result = asyncio.run(scenario())

    assert result["status"] == "invalid_target"
    assert calls == 0


def test_bridge_http_api_requires_bearer_token_and_returns_redacted_health() -> None:
    async def scenario() -> tuple[int, dict[str, Any]]:
        bridge = WeChatSessionBridge()
        bridge.capture(capture_request())
        app = create_wechat_bridge_app("x" * 32, bridge=bridge)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bridge.local",
        ) as client:
            denied = await client.get("/health")
            allowed = await client.get(
                "/health",
                headers={"Authorization": f"Bearer {'x' * 32}"},
            )
        await bridge.aclose()
        return denied.status_code, allowed.json()

    denied_status, status = asyncio.run(scenario())

    assert denied_status == 401
    assert status["sessions"][0]["status"] == "valid"
    for secret in SECRET_VALUES:
        assert secret not in str(status)
