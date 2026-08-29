from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from typing import Any

import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platforms import PLATFORM_HANDLERS
from app.crawlers.platforms.bilibili import extract_wbi_mixin_key, sign_wbi_params
from app.crawlers.platforms.registry import (
    extract_wechat_channels_mobile_feed_id,
    identify_url,
    normalize_media_name,
    weibo_bid_to_mid,
)
from app.crawlers.platforms.toutiao import parse_stats as parse_toutiao_stats
from app.crawlers.platforms.haokan import parse_ssr_stats as parse_haokan_stats
from app.crawlers.platforms.xiaohongshu import parse_stats as parse_xhs_stats
from app.crawlers.platforms.wechat_channels import parse_formatted_count
from app.models.engagement import EngagementResult, EngagementStats


class FakeResponse:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        text: str = "",
        status_code: int = 200,
        cookies: dict[str, str] | None = None,
        url: str = "",
    ) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.cookies = cookies or {}
        self.url = url

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def test_all_supported_platforms_have_independent_handlers() -> None:
    assert set(PLATFORM_HANDLERS) == {
        "douyin",
        "toutiao",
        "wechat",
        "wechat_channels",
        "xiaohongshu",
        "haokan",
        "kuaishou",
        "bilibili",
        "weibo",
    }


class FakeClient:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class LeaseAwareClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.invalidations: list[str] = []
        self.lease_count = 0

    @asynccontextmanager
    async def lease_scope(self):
        self.lease_count += 1
        yield

    def invalidate_active_lease(self, reason: str) -> None:
        self.invalidations.append(reason)


def test_protocol_retries_semantic_block_and_reports_attempts(monkeypatch) -> None:
    calls = 0

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return EngagementResult(
                platform="weibo",
                canonical_url="https://m.weibo.cn/detail/12345678",
                work_id="12345678",
                coverage="blocked",
                reason="HTTP 200 payload requested captcha",
            )
        return EngagementResult(
            platform="weibo",
            canonical_url="https://m.weibo.cn/detail/12345678",
            work_id="12345678",
            coverage="partial",
            stats=EngagementStats(likes=7),
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "weibo", handler)
    client = LeaseAwareClient()
    crawler = EngagementCrawler(
        client=client,
        max_protocol_attempts=3,
        protocol_retry_base_seconds=0,
    )

    result = asyncio.run(crawler.fetch_interactions(
        "https://m.weibo.cn/detail/12345678",
        "weibo",
    ))

    assert result.stats.likes == 7
    assert result.protocol_attempts == 2
    assert calls == 2
    assert client.lease_count == 2
    assert len(client.invalidations) == 1


def test_protocol_does_not_retry_intrinsic_unsupported_result(monkeypatch) -> None:
    calls = 0

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        nonlocal calls
        calls += 1
        return EngagementResult(
            platform="weibo",
            canonical_url="https://m.weibo.cn/detail/12345678",
            work_id="12345678",
            coverage="unsupported",
            reason="caller session is required",
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "weibo", handler)
    client = LeaseAwareClient()
    crawler = EngagementCrawler(
        client=client,
        max_protocol_attempts=3,
        protocol_retry_base_seconds=0,
    )

    result = asyncio.run(crawler.fetch_interactions(
        "https://m.weibo.cn/detail/12345678",
        "weibo",
    ))

    assert result.coverage == "unsupported"
    assert result.protocol_attempts == 1
    assert calls == 1
    assert client.invalidations == []


def test_platform_protocol_attempt_cap_avoids_known_useless_retries(monkeypatch) -> None:
    calls = 0

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        nonlocal calls
        calls += 1
        return EngagementResult(
            platform="toutiao",
            canonical_url="https://www.toutiao.com/article/1234567890/",
            work_id="1234567890",
            coverage="partial",
            reason="SSR missing itemCounter",
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "toutiao", handler)
    client = LeaseAwareClient()
    crawler = EngagementCrawler(
        client=client,
        max_protocol_attempts=3,
        platform_protocol_max_attempts={"toutiao": 1},
        protocol_retry_base_seconds=0,
    )

    result = asyncio.run(crawler.fetch_interactions(
        "https://www.toutiao.com/article/1234567890/",
        "toutiao",
    ))

    assert result.protocol_attempts == 1
    assert calls == 1
    assert client.lease_count == 1
    assert len(client.invalidations) == 1


def test_platform_protocol_attempt_override_can_exceed_global_default(monkeypatch) -> None:
    calls = 0

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        nonlocal calls
        calls += 1
        if calls < 5:
            return EngagementResult(
                platform="douyin",
                canonical_url="https://www.douyin.com/video/7665718789363309172",
                work_id="7665718789363309172",
                coverage="blocked",
                reason="empty detail payload",
            )
        return EngagementResult(
            platform="douyin",
            canonical_url="https://www.douyin.com/video/7665718789363309172",
            work_id="7665718789363309172",
            coverage="partial",
            stats=EngagementStats(likes=3),
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "douyin", handler)
    crawler = EngagementCrawler(
        client=LeaseAwareClient(),
        max_protocol_attempts=3,
        platform_protocol_max_attempts={"douyin": 5},
        protocol_retry_base_seconds=0,
    )

    result = asyncio.run(crawler.fetch_interactions(
        "https://www.douyin.com/video/7665718789363309172",
        "douyin",
    ))

    assert result.stats.likes == 3
    assert result.protocol_attempts == 5
    assert calls == 5


def test_douyin_preferred_proxy_uses_stable_direct_egress(monkeypatch) -> None:
    class DirectRecoveryClient(LeaseAwareClient):
        proxy_mode = "prefer"

        def __init__(self) -> None:
            super().__init__()
            self.in_direct_scope = False
            self.direct_count = 0

        @asynccontextmanager
        async def direct_scope(self):
            self.direct_count += 1
            self.in_direct_scope = True
            try:
                yield
            finally:
                self.in_direct_scope = False

    client = DirectRecoveryClient()

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        assert client.in_direct_scope
        return EngagementResult(
            platform="douyin",
            canonical_url="https://www.douyin.com/video/7665718789363309172",
            work_id="7665718789363309172",
            coverage="partial",
            stats=EngagementStats(likes=12),
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "douyin", handler)
    crawler = EngagementCrawler(
        client=client,
        platform_protocol_max_attempts={"douyin": 2},
        protocol_retry_base_seconds=10,
    )

    result = asyncio.run(crawler.fetch_interactions(
        "https://www.douyin.com/video/7665718789363309172",
        "douyin",
    ))

    assert result.stats.likes == 12
    assert result.protocol_attempts == 1
    assert client.lease_count == 1
    assert client.direct_count == 1
    assert client.invalidations == []


def test_douyin_circuit_breaker_fails_fast_after_systemic_failures(monkeypatch) -> None:
    calls = 0

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        nonlocal calls
        calls += 1
        work_id = str(args[2])
        return EngagementResult(
            platform="douyin",
            canonical_url=f"https://www.douyin.com/video/{work_id}",
            work_id=work_id,
            coverage="blocked",
            reason="temporary upstream block",
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "douyin", handler)
    crawler = EngagementCrawler(
        client=LeaseAwareClient(),
        max_protocol_attempts=1,
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=30,
    )

    first = asyncio.run(crawler.fetch_interactions(
        "https://www.douyin.com/video/7665718789363309171", "douyin"
    ))
    second = asyncio.run(crawler.fetch_interactions(
        "https://www.douyin.com/video/7665718789363309172", "douyin"
    ))
    third = asyncio.run(crawler.fetch_interactions(
        "https://www.douyin.com/video/7665718789363309173", "douyin"
    ))

    assert first.coverage == second.coverage == third.coverage == "blocked"
    assert calls == 2
    assert "熔断" in third.reason


def test_toutiao_ssr_probe_bypasses_preferred_proxy() -> None:
    class DirectAwareClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(FakeResponse(text="<html></html>"))
            self.direct_scopes = 0

        @asynccontextmanager
        async def direct_scope(self):
            self.direct_scopes += 1
            yield

    client = DirectAwareClient()
    crawler = EngagementCrawler(client=client)

    result = asyncio.run(crawler.fetch_interactions(
        "https://www.toutiao.com/article/1234567890/",
        "toutiao",
    ))

    assert result.coverage == "partial"
    assert client.direct_scopes == 1


def test_browser_fallback_retries_unusable_result(monkeypatch) -> None:
    class RetryBrowserFallback:
        def __init__(self) -> None:
            self.calls = []

        async def fetch(self, url, platform, work_id, **kwargs):
            self.calls.append((url, platform, work_id, kwargs))
            if len(self.calls) == 1:
                return EngagementResult(
                    platform=platform,
                    canonical_url=url,
                    work_id=work_id,
                    coverage="unsupported",
                    reason="empty browser response",
                )
            return EngagementResult(
                platform=platform,
                canonical_url=url,
                work_id=work_id,
                coverage="partial",
                stats=EngagementStats(likes=9),
            )

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        return EngagementResult(
            platform="toutiao",
            canonical_url="https://www.toutiao.com/article/12345678/",
            work_id="12345678",
            coverage="blocked",
            reason="protocol blocked",
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "toutiao", handler)
    browser = RetryBrowserFallback()
    crawler = EngagementCrawler(
        client=LeaseAwareClient(),
        browser_fallback=browser,
        max_browser_attempts=2,
    )

    result = asyncio.run(crawler.fetch_interactions(
        "https://www.toutiao.com/article/12345678/",
        "toutiao",
    ))

    assert result.stats.likes == 9
    assert len(browser.calls) == 2


def test_xiaohongshu_interaction_wall_does_not_retry_fixed_direct_exit(
    monkeypatch,
) -> None:
    calls = 0
    note_id = "6a5585c000000000080326ac"

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        nonlocal calls
        calls += 1
        return EngagementResult(
            platform="xiaohongshu",
            canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            work_id=note_id,
            coverage="blocked",
            reason="note-page wall",
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "xiaohongshu", handler)
    crawler = EngagementCrawler(
        client=LeaseAwareClient(),
        max_protocol_attempts=3,
    )

    result = asyncio.run(crawler.fetch_interactions(
        f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=token",
        "xiaohongshu",
    ))

    assert result.coverage == "blocked"
    assert result.protocol_attempts == 1
    assert calls == 1


def test_xiaohongshu_interaction_wall_retries_with_required_proxy(
    monkeypatch,
) -> None:
    calls = 0
    note_id = "6a5585c000000000080326ac"

    async def handler(*args: Any, **kwargs: Any) -> EngagementResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return EngagementResult(
                platform="xiaohongshu",
                canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
                work_id=note_id,
                coverage="blocked",
                reason="note-page wall",
            )
        return EngagementResult(
            platform="xiaohongshu",
            canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
            work_id=note_id,
            coverage="partial",
            stats=EngagementStats(likes=9),
        )

    monkeypatch.setitem(PLATFORM_HANDLERS, "xiaohongshu", handler)
    client = LeaseAwareClient()
    client.proxy_mode = "required"
    crawler = EngagementCrawler(
        client=client,
        proxy_provider=object(),
        max_protocol_attempts=3,
        protocol_retry_base_seconds=10,
    )

    result = asyncio.run(crawler.fetch_interactions(
        f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=token",
        "xiaohongshu",
    ))

    assert result.stats.likes == 9
    assert result.protocol_attempts == 2
    assert calls == 2
    assert client.lease_count == 2
    assert len(client.invalidations) == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.bilibili.com/video/BV1abc123", ("bilibili", "BV1abc123")),
        ("https://www.bilibili.com/video/av12345", ("bilibili", "12345")),
        ("https://www.bilibili.com/read/cv34832696/", ("bilibili", "article:34832696")),
        ("https://www.bilibili.com/read/mobile?id=34832696", ("bilibili", "article:34832696")),
        ("https://www.bilibili.com/opus/907932915033178114", ("bilibili", "opus:907932915033178114")),
        ("https://live.bilibili.com/22595201#pub=1787011298", ("bilibili", "")),
        ("https://m.weibo.cn/detail/5301066679190033", ("weibo", "5301066679190033")),
        (
            "https://video.weibo.com/show?fid=1034:5301066679190033",
            ("weibo", "5301066679190033"),
        ),
        (
            "https://weibo.com/tv/show/1034:5301066679190033",
            ("weibo", "5301066679190033"),
        ),
        ("http://weibo.com/6539142196/RdVPmEYKD", ("weibo", "5333205569766651")),
        ("https://www.xiaohongshu.com/explore/6a5585c000000000080326ac", ("xiaohongshu", "6a5585c000000000080326ac")),
        ("https://haokan.baidu.com/v?vid=327646248367276281", ("haokan", "327646248367276281")),
        ("https://www.douyin.com/video/7665718789363309172", ("douyin", "7665718789363309172")),
        ("https://www.toutiao.com/article/1234567890/", ("toutiao", "1234567890")),
        ("https://www.toutiao.com/i7675062618096288275/", ("toutiao", "7675062618096288275")),
        ("https://www.kuaishou.com/short-video/abc", ("kuaishou", "abc")),
        ("https://c.kuaishou.com/fw/photo/3x4zebgce2jutx2", ("kuaishou", "3x4zebgce2jutx2")),
        ("https://mp.weixin.qq.com/s?mid=2247504578", ("wechat", "2247504578")),
        ("https://mp.weixin.qq.com/s/XKB0QLWfxHAJrOo-QvHsVw", ("wechat", "XKB0QLWfxHAJrOo-QvHsVw")),
        ("https://weixin.qq.com/sph/AoPX5bEBDd", ("wechat_channels", "AoPX5bEBDd")),
        ("https://channels.weixin.qq.com/finder-preview/pages/sph?id=Ali0QjN99U", ("wechat_channels", "Ali0QjN99U")),
        (
            "https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?"
            "api=openFinderView&extInfo=%7B%22action%22%3A%22openFinderFeed%22%2C"
            "%22feedID%22%3A%22export%2FUzFfBgAAxN6jAAkGAmPvk8zT4DCJorvgXiwL15tbF2yqxVCjFw%22%7D",
            (
                "wechat_channels",
                "export/UzFfBgAAxN6jAAkGAmPvk8zT4DCJorvgXiwL15tbF2yqxVCjFw",
            ),
        ),
    ],
)
def test_identify_url(url: str, expected: tuple[str, str]) -> None:
    assert identify_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        (
            "https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?"
            "api=openFinderView&extInfo=not-json"
        ),
        (
            "https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?"
            "api=openFinderView&extInfo=%7B%22action%22%3A%22openFinderFeed%22%2C"
            "%22feedID%22%3A%22https%3A%2F%2Fexample.com%22%7D"
        ),
        (
            "https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?"
            "api=openFinderView&extInfo=%7B%22action%22%3A%22other%22%2C"
            "%22feedID%22%3A%22export%2Fabc12345%22%7D"
        ),
    ],
)
def test_mobile_wechat_channels_url_rejects_invalid_ext_info(url: str) -> None:
    assert extract_wechat_channels_mobile_feed_id(url) == ""


def test_weibo_desktop_bid_conversion() -> None:
    assert weibo_bid_to_mid("RdVPmEYKD") == "5333205569766651"


def test_bilibili_article_interactions_use_public_viewinfo() -> None:
    client = FakeClient(FakeResponse(payload={
        "code": 0,
        "data": {
            "stats": {
                "view": 7163,
                "favorite": 709,
                "like": 422,
                "reply": 23,
                "share": 34,
                "coin": 68,
                "dynamic": 42,
            },
        },
    }))

    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        "https://www.bilibili.com/read/cv34832696/",
        "B站",
    ))

    assert result.work_id == "cv34832696"
    assert result.stats.views == 7163
    assert result.stats.likes == 422
    assert result.stats.comments == 23
    assert result.stats.reposts == 42
    assert client.calls[0][0].endswith("/x/article/viewinfo")


def test_bilibili_article_comments_use_type_12_without_detail_request() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "code": 0,
            "data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyz0123456789abcdefgh.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/ZYXWVUTSRQPONMLKJIHGFEDCBA987654321zyxwvutsrqponmlk.png",
            }},
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {
                "cursor": {"next": 2, "all_count": 23},
                "replies": [{
                    "rpid": "comment-1",
                    "member": {"uname": "专栏读者"},
                    "content": {"message": "专栏评论"},
                }],
            },
        }),
    )

    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://www.bilibili.com/read/cv34832696/",
        "bilibili",
        1,
    ))

    assert result.comments[0].comment_id == "comment-1"
    assert result.comments[0].text == "专栏评论"
    assert result.total_comments == 23
    assert result.next_page is None
    assert len(client.calls) == 2
    assert client.calls[1][1]["params"]["type"] == "12"
    assert client.calls[1][1]["params"]["oid"] == "34832696"


def test_bilibili_opus_uses_page_comment_target_and_stats() -> None:
    initial_state = {
        "detail": {
            "id_str": "907932915033178114",
            "basic": {"comment_type": 12, "comment_id_str": "33179525"},
            "modules": [{
                "module_stat": {
                    "forward": {"count": 1},
                    "comment": {"count": 2},
                    "like": {"count": 3},
                    "coin": {"count": 4},
                    "favorite": {"count": 5},
                },
            }],
        },
    }
    html = f"<script>window.__INITIAL_STATE__ = {json.dumps(initial_state)};</script>"
    client = FakeClient(
        FakeResponse(text=html),
        FakeResponse(payload={
            "code": 0,
            "data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyz0123456789abcdefgh.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/ZYXWVUTSRQPONMLKJIHGFEDCBA987654321zyxwvutsrqponmlk.png",
            }},
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {"cursor": {"all_count": 2, "next": 0}, "replies": []},
        }),
    )

    result = asyncio.run(EngagementCrawler(client=client).fetch(
        "https://www.bilibili.com/opus/907932915033178114"
    ))

    assert result.stats.likes == 3
    assert result.stats.comments == 2
    assert result.stats.favorites == 5
    assert client.calls[0][1]["discard_cookies"] is True
    assert client.calls[2][1]["params"]["type"] == "12"
    assert client.calls[2][1]["params"]["oid"] == "33179525"


def test_bilibili_live_url_is_outside_content_scope() -> None:
    client = FakeClient()

    with pytest.raises(ValueError, match="cannot extract bilibili content id"):
        asyncio.run(EngagementCrawler(client=client).fetch_interactions(
            "https://live.bilibili.com/22595201",
            "B站",
        ))

    assert client.calls == []


@pytest.mark.parametrize(
    ("media_name", "expected"),
    [
        ("douyin", "douyin"),
        ("抖音", "douyin"),
        ("今日头条", "toutiao"),
        ("公众号", "wechat"),
        ("微信视频号", "wechat_channels"),
        ("小红书", "xiaohongshu"),
        ("好看视频", "haokan"),
        ("快手", "kuaishou"),
        ("B 站", "bilibili"),
        ("哔哩哔哩", "bilibili"),
        ("微博", "weibo"),
    ],
)
def test_normalize_media_name(media_name: str, expected: str) -> None:
    assert normalize_media_name(media_name) == expected


def test_media_name_must_match_url_before_request() -> None:
    client = FakeClient()

    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(EngagementCrawler(client=client).fetch_interactions(
            "https://www.bilibili.com/video/BVgood",
            "微博",
        ))

    assert client.calls == []


def test_wechat_channels_public_preview_returns_displayed_counters() -> None:
    client = FakeClient(FakeResponse(payload={
        "errCode": 0,
        "errMsg": "",
        "data": {
            "feedInfo": {
                "description": "视频号测试视频",
                "likeCountFmt": "5.2万",
                "commentCountFmt": "4044",
                "forwardCountFmt": "3.8万",
                "favCountFmt": "10万+",
            },
            "sceneInfo": {"dynamicExportId": "export/id"},
            "errMsg": {"type": 0},
        },
    }))

    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        "https://weixin.qq.com/sph/AoPX5bEBDd",
        "视频号",
    ))

    assert result.platform == "wechat_channels"
    assert result.stats.likes == 52_000
    assert result.stats.comments == 4_044
    assert result.stats.shares == 38_000
    assert result.stats.favorites == 100_000
    assert len(client.calls) == 1
    assert client.calls[0][0].endswith("/finder-preview/api/feed/get_feed_info")
    assert client.calls[0][1]["json"]["shortUri"] == "AoPX5bEBDd"


def test_wechat_channels_comment_api_reports_count_without_fake_bodies() -> None:
    client = FakeClient(FakeResponse(payload={
        "errCode": 0,
        "errMsg": "",
        "data": {
            "feedInfo": {
                "description": "视频号测试视频",
                "commentCountFmt": "1530",
            },
            "sceneInfo": {"dynamicExportId": "export/id"},
            "errMsg": {"type": 0},
        },
    }))

    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://weixin.qq.com/sph/Ali0QjN99U",
        "wechat_channels",
        1,
    ))

    assert result.coverage == "unsupported"
    assert result.comments == []
    assert result.total_comments == 1530
    assert "微信客户端" in result.reason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1), ("1.2万", 12_000), ("3.8W", 38_000), ("2亿+", 200_000_000), ("--", None)],
)
def test_wechat_channels_formatted_count(raw: str, expected: int | None) -> None:
    assert parse_formatted_count(raw) == expected


def test_bilibili_interactions_skip_comment_endpoint() -> None:
    client = FakeClient(FakeResponse(payload={
        "code": 0,
        "data": {
            "bvid": "BVgood",
            "aid": 123,
            "stat": {"view": 100, "like": 20, "reply": 3},
        },
    }))

    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        "https://www.bilibili.com/video/BVgood",
        "B站",
    ))

    assert result.stats.views == 100
    assert "comments" not in result.model_dump()
    assert [call[0] for call in client.calls] == [
        "https://api.bilibili.com/x/web-interface/view"
    ]


def test_bilibili_comments_use_requested_page() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "code": 0,
            "data": {"bvid": "BVgood", "aid": 123, "stat": {}},
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyz0123456789abcdefgh.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/ZYXWVUTSRQPONMLKJIHGFEDCBA987654321zyxwvutsrqponmlk.png",
            }},
        }),
        FakeResponse(payload={"code": 0, "data": {"cursor": {"next": 2, "all_count": 70, "pagination_reply": {"next_offset": "token-2"}}, "replies": []}}),
        FakeResponse(payload={"code": 0, "data": {"cursor": {"next": 3, "all_count": 70, "pagination_reply": {"next_offset": "token-3"}}, "replies": []}}),
        FakeResponse(payload={"code": 0, "data": {"cursor": {"next": 4, "all_count": 70, "pagination_reply": {"next_offset": "token-4"}}, "replies": []}}),
    )

    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://www.bilibili.com/video/BVgood",
        "bilibili",
        3,
    ))

    assert result.page == 3
    assert result.next_page == 4
    assert result.total_comments == 70
    assert client.calls[1][0] == "https://api.bilibili.com/x/web-interface/nav"
    assert client.calls[4][0] == "https://api.bilibili.com/x/v2/reply/wbi/main"
    assert client.calls[3][1]["params"]["pagination_str"] == '{"offset":"token-2"}'
    assert client.calls[4][1]["params"]["pagination_str"] == '{"offset":"token-3"}'


def test_bilibili_comments_resume_from_known_cursor_without_replaying_pages() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "code": 0,
            "data": {"bvid": "BVgood", "aid": 123, "stat": {}},
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyz0123456789abcdefgh.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/ZYXWVUTSRQPONMLKJIHGFEDCBA987654321zyxwvutsrqponmlk.png",
            }},
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {
                "cursor": {
                    "next": 4,
                    "all_count": 70,
                    "pagination_reply": {"next_offset": "token-4"},
                },
                "replies": [],
            },
        }),
    )

    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://www.bilibili.com/video/BVgood",
        "bilibili",
        3,
        cursor="token-3",
    ))

    assert result.next_page == 4
    assert result.next_cursor == "token-4"
    assert len(client.calls) == 3
    assert client.calls[2][1]["params"]["pagination_str"] == '{"offset":"token-3"}'


def test_bilibili_does_not_relabel_last_cursor_page() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "code": 0,
            "data": {"bvid": "BVgood", "aid": 123, "stat": {}},
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyz0123456789abcdefgh.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/ZYXWVUTSRQPONMLKJIHGFEDCBA987654321zyxwvutsrqponmlk.png",
            }},
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {
                "cursor": {"next": 0, "all_count": 1},
                "replies": [{"rpid": 1, "content": {"message": "最后一页"}}],
            },
        }),
    )

    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://www.bilibili.com/video/BVgood",
        "bilibili",
        2,
    ))

    assert result.comments == []
    assert result.next_page is None
    assert result.total_comments == 1


def test_bilibili_current_wbi_table_matches_browser_sample() -> None:
    mixin_key = extract_wbi_mixin_key({
        "code": -101,
        "data": {"wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        }},
    })
    signed = sign_wbi_params(
        {
            "oid": "455017605",
            "type": "1",
            "mode": "3",
            "pagination_str": '{"offset":""}',
            "plat": "1",
            "seek_rpid": "",
            "web_location": "1315875",
        },
        mixin_key=mixin_key,
        wts=1787158788,
    )

    assert mixin_key == "ea1db124af3c7062474693fa704f4ff8"
    assert signed["w_rid"] == "4233c504c4a2ed5d38c42602d2f4704b"


def test_toutiao_comments_map_page_to_offset() -> None:
    client = FakeClient(FakeResponse(payload={
        "err_no": 0,
        "total_number": 61,
        "has_more": True,
        "offset": 60,
        "data": [],
    }))

    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://www.toutiao.com/article/7557632662635840036/",
        "头条",
        3,
    ))

    assert result.page == 3
    assert result.next_page == 4
    assert result.total_comments == 61
    assert client.calls[0][1]["params"]["offset"] == "40"


def test_cursor_platform_does_not_relabel_last_page() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "status_code": 0,
            "has_more": 1,
            "cursor": 20,
            "comments": [{"cid": "c1", "text": "第一页"}],
        }, text='{"status_code":0}'),
        FakeResponse(payload={
            "status_code": 0,
            "has_more": 0,
            "cursor": 40,
            "comments": [{"cid": "c2", "text": "最后一页"}],
        }, text='{"status_code":0}'),
    )

    result = asyncio.run(EngagementCrawler(
        client=client,
        cookies="sessionid=caller-session",
    ).fetch_comments(
        "https://www.douyin.com/video/7665718789363309172",
        "抖音",
        3,
    ))

    assert result.page == 3
    assert result.comments == []
    assert result.next_page is None
    assert len(client.calls) == 2


def test_bilibili_fetches_stats_and_comments() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "code": 0,
            "data": {
                "bvid": "BVgood",
                "aid": 123,
                "stat": {"view": 100, "like": 20, "reply": 3, "share": 4, "favorite": 5, "coin": 6, "danmaku": 7},
            },
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/abcdefghijklmnopqrstuvwxyz0123456789abcdefgh.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/ZYXWVUTSRQPONMLKJIHGFEDCBA987654321zyxwvutsrqponmlk.png",
                },
            },
        }),
        FakeResponse(payload={
            "code": 0,
            "data": {
                "cursor": {
                    "next": 2,
                    "all_count": 21,
                    "is_end": False,
                    "pagination_reply": {"next_offset": "token-2"},
                },
                "replies": [{
                    "rpid": 9,
                    "ctime": 1700000000,
                    "like": 8,
                    "rcount": 2,
                    "member": {"uname": "alice"},
                    "content": {"message": "评论"},
                }],
            },
        }),
    )
    result = asyncio.run(EngagementCrawler(client=client).fetch("https://www.bilibili.com/video/BVgood"))

    assert result.coverage == "partial"
    assert "当前公开页" in result.reason
    assert result.stats.model_dump() == {
        "views": 100,
        "likes": 20,
        "comments": 3,
        "shares": 4,
        "favorites": 5,
        "coins": 6,
        "danmaku": 7,
        "reposts": None,
        "recommendations": None,
    }
    assert result.comments[0].text == "评论"
    assert result.next_cursor == "2"


def test_platform_cookie_is_not_sent_to_unrelated_domains() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "code": 0,
            "data": {"bvid": "BVgood", "aid": 123, "stat": {}},
        }),
        FakeResponse(payload={"code": 0, "data": {"page": {}, "replies": []}}),
    )
    asyncio.run(EngagementCrawler(client=client, cookies="sessionid=private").fetch(
        "https://www.bilibili.com/video/BVgood"
    ))

    assert "Cookie" not in client.calls[0][1]["headers"]
    assert "Cookie" not in client.calls[1][1]["headers"]


def test_douyin_fetches_detail_stats_and_comments_with_caller_cookie() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "status_code": 0,
            "aweme_detail": {
                "statistics": {
                    "play_count": 1000,
                    "digg_count": 20,
                    "comment_count": 3,
                    "share_count": 4,
                    "collect_count": 5,
                },
            },
        }, text='{"status_code":0,"aweme_detail":{}}'),
        FakeResponse(payload={
            "status_code": 0,
            "cursor": 5,
            "comments": [{
                "cid": "c1",
                "text": "抖音评论",
                "digg_count": 8,
                "reply_comment_total": 2,
                "user": {"nickname": "用户"},
            }],
        }, text='{"status_code":0,"comments":[]}'),
    )
    result = asyncio.run(EngagementCrawler(client=client, cookies="UIFID_TEMP=caller-session").fetch(
        "https://www.douyin.com/video/7665718789363309172"
    ))

    assert result.coverage == "partial"
    assert result.stats.views == 1000
    assert result.stats.likes == 20
    assert result.stats.favorites == 5
    assert result.comments[0].author == "用户"
    assert result.next_cursor == "5"
    assert client.calls[0][0].endswith("/aweme/v1/web/aweme/detail/")
    assert client.calls[0][1]["headers"]["Cookie"] == "UIFID_TEMP=caller-session"


def test_douyin_stats_are_partial_when_visitor_comment_endpoint_returns_empty() -> None:
    client = FakeClient(
        FakeResponse(payload={
            "status_code": 0,
            "aweme_detail": {"statistics": {"digg_count": 9, "comment_count": 2}},
        }, text='{"status_code":0,"aweme_detail":{}}'),
        FakeResponse(text=""),
    )
    result = asyncio.run(EngagementCrawler(client=client, cookies="sessionid=caller-session").fetch(
        "https://www.douyin.com/video/7665718789363309172"
    ))

    assert result.coverage == "partial"
    assert result.stats.likes == 9
    assert result.comments == []
    assert "空包" in result.reason


def test_douyin_permanent_unavailable_result_is_not_retried_or_sent_to_browser() -> None:
    class BrowserShouldNotRun:
        calls = 0

        async def fetch(self, *args: Any, **kwargs: Any) -> EngagementResult:
            self.calls += 1
            raise AssertionError("browser fallback must not run for Douyin")

    browser = BrowserShouldNotRun()
    client = FakeClient(FakeResponse(
        payload={
            "status_code": 0,
            "filter_detail": {
                "filter_reason": "status_deleted",
                "detail_msg": "因作品权限或已被删除，无法观看",
            },
        },
        text='{"status_code":0,"filter_detail":{}}',
    ))
    result = asyncio.run(EngagementCrawler(
        client=client,
        cookies="ttwid=caller-session",
        browser_fallback=browser,  # type: ignore[arg-type]
        platform_protocol_max_attempts={"douyin": 2},
    ).fetch_interactions(
        "https://www.douyin.com/video/7665718789363309172",
        "抖音",
    ))

    assert result.coverage == "unsupported"
    assert result.protocol_attempts == 1
    assert "status_deleted" in result.reason
    assert len(client.calls) == 1
    assert browser.calls == 0


def test_douyin_hidden_play_count_is_not_reported_as_real_zero() -> None:
    client = FakeClient(FakeResponse(payload={
        "status_code": 0,
        "aweme_detail": {
            "statistics": {
                "play_count": 0,
                "digg_count": 20,
                "comment_count": 3,
            },
        },
    }, text='{"status_code":0,"aweme_detail":{}}'))

    result = asyncio.run(EngagementCrawler(
        client=client,
        cookies="sessionid=caller-session",
    ).fetch_interactions(
        "https://www.douyin.com/video/7665718789363309172",
        "抖音",
    ))

    assert result.stats.views is None
    assert result.stats.likes == 20


def test_weibo_fetches_stats_and_hot_comments() -> None:
    client = FakeClient(
        FakeResponse(payload={"ok": 1, "data": {"attitudes_count": 11, "comments_count": 12, "reposts_count": 13}}),
        FakeResponse(payload={
            "ok": 1,
            "data": {
                "max_id": 99,
                "data": [{"id": 1, "text": "<b>观点</b>", "like_count": 3, "total_number": 2, "user": {"screen_name": "bob"}}],
            },
        }),
    )
    result = asyncio.run(EngagementCrawler(client=client).fetch("https://m.weibo.cn/detail/5301066679190033"))

    assert result.coverage == "partial"
    assert result.stats.likes == 11
    assert result.stats.comments == 12
    assert result.stats.reposts == 13
    assert result.comments[0].author == "bob"
    assert result.comments[0].text == "观点"
    assert result.next_cursor == "99"


def test_weibo_desktop_uses_bid_with_anonymous_visitor_session() -> None:
    visitor_page = FakeResponse(
        text='<script>var request_id = "0123456789abcdef0123456789abcdef";</script>',
        url="https://passport.weibo.com/visitor/visitor?entry=miniblog",
    )
    generated = FakeResponse(text=(
        'window.visitor_gray_callback && visitor_gray_callback('
        '{"retcode":20000000,"msg":"succ","data":'
        '{"sub":"guest-sub","subp":"guest-subp"}});'
    ))
    detail = FakeResponse(payload={
        "idstr": "5336588076712748",
        "mblogid": "RflP1sai8",
        "attitudes_count": 11,
        "comments_count": 12,
        "reposts_count": 13,
    })
    client = FakeClient(visitor_page, generated, detail)

    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        "http://weibo.com/6660086860/RflP1sai8",
        "微博",
    ))

    assert result.coverage == "partial"
    assert result.work_id == "5336588076712748"
    assert result.stats.likes == 11
    assert result.stats.comments == 12
    assert result.stats.reposts == 13
    assert client.calls[1][0].endswith("/visitor/genvisitor2")
    assert client.calls[2][1]["params"]["id"] == "RflP1sai8"
    assert result.source == "weibo.com/ajax/statuses/show"


def test_weibo_video_maps_fid_to_mid_with_component_api() -> None:
    visitor_page = FakeResponse(
        text='<script>var request_id = "0123456789abcdef0123456789abcdef";</script>',
        url="https://passport.weibo.com/visitor/visitor?entry=krvideo",
    )
    generated = FakeResponse(text=(
        'visitor_gray_callback({"retcode":20000000,"data":'
        '{"sub":"guest-sub","subp":"guest-subp"}});'
    ))
    detail = FakeResponse(payload={
        "code": "100000",
        "msg": "succ",
        "data": {
            "Component_Play_Playinfo": {
                "mid": 5336419992338781,
                "attitudes_count": 3,
                "comments_count": 4,
                "reposts_count": 5,
            },
        },
    })
    client = FakeClient(visitor_page, generated, detail)

    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        "https://video.weibo.com/show?fid=1034:5336419900784646",
        "微博",
    ))

    assert result.work_id == "5336419992338781"
    assert result.stats.model_dump(exclude_none=True) == {
        "likes": 3,
        "comments": 4,
        "reposts": 5,
    }
    assert client.calls[2][0].endswith("/tv/api/component")
    assert "1034:5336419900784646" in client.calls[2][1]["data"]["data"]
    assert result.source == "weibo.com/tv/api/component"


def test_weibo_missing_video_is_permanent() -> None:
    client = FakeClient(
        FakeResponse(
            text='<script>var request_id = "0123456789abcdef0123456789abcdef";</script>',
            url="https://passport.weibo.com/visitor/visitor?entry=krvideo",
        ),
        FakeResponse(text=(
            'visitor_gray_callback({"retcode":20000000,"data":'
            '{"sub":"guest-sub"}});'
        )),
        FakeResponse(payload={
            "code": "100000",
            "msg": "succ",
            "data": {"Component_Play_Playinfo": []},
        }),
    )

    result = asyncio.run(EngagementCrawler(
        client=client,
        max_protocol_attempts=3,
    ).fetch_interactions(
        "https://video.weibo.com/show?fid=1034:1000000000000000",
        "微博",
    ))

    assert result.coverage == "unsupported"
    assert result.protocol_attempts == 1
    assert "不存在或无查看权限" in result.reason
    assert len(client.calls) == 3


def test_weibo_unavailable_desktop_status_is_permanent() -> None:
    client = FakeClient(
        FakeResponse(
            text='<script>var request_id = "0123456789abcdef0123456789abcdef";</script>',
            url="https://passport.weibo.com/visitor/visitor?entry=miniblog",
        ),
        FakeResponse(text=(
            'visitor_gray_callback({"retcode":20000000,"data":'
            '{"sub":"guest-sub"}});'
        )),
        FakeResponse(payload={
            "ok": 0,
            "message": "该微博不存在",
            "error_code": 20101,
        }),
    )

    result = asyncio.run(EngagementCrawler(
        client=client,
        max_protocol_attempts=3,
    ).fetch_interactions(
        "http://weibo.com/3772043430/RfwH1lUkh",
        "微博",
    ))

    assert result.coverage == "unsupported"
    assert result.protocol_attempts == 1
    assert "不存在" in result.reason
    assert len(client.calls) == 3


def test_weibo_session_cookie_and_cursor_type_support_deep_pages() -> None:
    def comment_page(comment_id: int, next_id: int, next_type: int) -> FakeResponse:
        return FakeResponse(payload={
            "ok": 1,
            "data": {
                "max_id": next_id,
                "max_id_type": next_type,
                "data": [{
                    "id": comment_id,
                    "text": f"第{comment_id}页",
                    "user": {"screen_name": "微博用户"},
                }],
            },
        })

    client = FakeClient(
        comment_page(1, 101, 1),
        comment_page(2, 202, 2),
        comment_page(3, 0, 0),
    )
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"weibo": "SUB=caller-session; XSRF-TOKEN=token%3D"},
    ).fetch_comments(
        "https://m.weibo.cn/detail/5301066679190033",
        "微博",
        3,
    ))

    assert result.comments[0].text == "第3页"
    assert client.calls[1][1]["params"]["max_id_type"] == 1
    assert client.calls[2][1]["params"]["max_id_type"] == 2
    assert client.calls[2][1]["headers"]["Cookie"].startswith("SUB=")
    assert client.calls[2][1]["headers"]["X-XSRF-TOKEN"] == "token="


def test_toutiao_fetches_comment_count_and_comments_without_signature() -> None:
    client = FakeClient(
        FakeResponse(text=(
            '%22itemCounter%22%3A%7B%22commentCount%22%3A28%2C%22diggCount%22%3A74%2C'
            '%22readCount%22%3A3297%2C%22shareCount%22%3A52%7D'
        )),
        FakeResponse(payload={
            "message": "success",
            "err_no": 0,
            "total_number": 28,
            "has_more": True,
            "offset": 5,
            "data": [{
                "comment": {
                    "id_str": "c1",
                    "user_name": "头条用户",
                    "text": "<b>头条评论</b>",
                    "create_time": 1700000000,
                    "digg_count": 4,
                    "reply_count": 2,
                },
            }],
        }),
    )
    result = asyncio.run(EngagementCrawler(client=client).fetch(
        "https://www.toutiao.com/article/7557632662635840036/"
    ))

    assert result.coverage == "partial"
    assert result.stats.likes == 74
    assert result.stats.comments == 28
    assert result.comments[0].author == "头条用户"
    assert result.comments[0].text == "头条评论"
    assert result.next_cursor == "5"
    assert "_signature" not in client.calls[1][1]["params"]


def test_toutiao_interactions_parse_ssr_counters_without_comment_request() -> None:
    client = FakeClient(FakeResponse(text=(
        '%22itemCounter%22%3A%7B%22commentCount%22%3A28%2C%22diggCount%22%3A74%2C'
        '%22readCount%22%3A3297%2C%22shareCount%22%3A52%2C%22showCount%22%3A167624%7D'
    )))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        "https://www.toutiao.com/article/7557632662635840036/",
        "头条",
    ))

    assert result.stats.likes == 74
    assert result.stats.views == 3297
    assert result.stats.comments == 28
    assert result.stats.shares == 52
    assert len(client.calls) == 1


def test_toutiao_stats_parser_accepts_plain_item_counter() -> None:
    stats = parse_toutiao_stats(
        '{"itemCounter":{"commentCount":3,"diggCount":4,"readCount":5,"shareCount":6}}'
    )
    assert stats.model_dump(exclude_none=True) == {
        "views": 5,
        "likes": 4,
        "comments": 3,
        "shares": 6,
    }


def test_haokan_comments_do_not_require_captured_signature() -> None:
    client = FakeClient(
        FakeResponse(text="<html>visitor bootstrap</html>"),
        FakeResponse(text=(
            '<meta name="description" content="示例,22220次播放,好看视频">'
            '<meta property="og:url" content="https://haokan.baidu.com/v?vid=327646248367276281">'
            '<div class="ssr-icon-comment">13</div>'
            '<div class="ssr-icon-like">257</div>'
        )),
        FakeResponse(payload={
            "status": 0,
            "data": {
                "comment_count": "13",
                "is_over": False,
                "list": [{"reply_id": "r1", "uname": "用户", "content": "内容", "like_count": "4", "reply_count": "2"}],
            },
        }),
    )
    result = asyncio.run(EngagementCrawler(client=client).fetch("https://haokan.baidu.com/v?vid=327646248367276281"))

    assert result.stats.views == 22220
    assert result.stats.likes == 257
    assert result.stats.comments == 13
    assert result.comments[0].comment_id == "r1"
    assert "hk_sign" not in client.calls[2][1]["params"]


def test_haokan_ssr_stats_require_target_video() -> None:
    text = (
        '<meta name="description" content="示例,110808次播放,好看视频">'
        '<meta property="og:url" content="https://haokan.baidu.com/v?vid=target-video">'
        '<div class="ssr-icon-comment">729</div>'
        '<div class="ssr-icon-like">620</div>'
    )

    assert parse_haokan_stats(text, "target-video").model_dump(exclude_none=True) == {
        "views": 110808,
        "likes": 620,
        "comments": 729,
    }
    assert parse_haokan_stats(text, "different-video").model_dump(exclude_none=True) == {}


def test_xiaohongshu_reads_ssr_stats_and_reports_signed_comments_as_partial() -> None:
    note_id = "6a5585c000000000080326ac"
    text = (
        '<script>{"noteDetailMap":{"' + note_id + '":{"note":{'
        '"interactInfo":{"collectedCount":"246","shareCount":"523",'
        '"likedCount":"3631","commentCount":"168"},'
        '"noteId":"' + note_id + '"}}}}</script>'
    )
    result = asyncio.run(EngagementCrawler(client=FakeClient(FakeResponse(text=text))).fetch(
        f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=token"
    ))

    assert result.coverage == "partial"
    assert result.stats.likes == 3631
    assert result.stats.favorites == 246
    assert result.stats.shares == 523
    assert result.stats.comments == 168
    assert result.comments == []


def test_xhs_stats_missing_note_is_empty_not_fabricated() -> None:
    assert parse_xhs_stats("<html></html>", "missing").model_dump() == {
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


def test_xiaohongshu_blocked_page_is_not_reported_as_partial_success() -> None:
    result = asyncio.run(EngagementCrawler(client=FakeClient(
        FakeResponse(text="challenge", status_code=403),
    )).fetch(
        "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac"
    ))

    assert result.coverage == "blocked"
    assert result.stats.likes is None


def test_douyin_anonymous_session_fetches_stats_and_comments() -> None:
    client = FakeClient(
        FakeResponse(text="<html>prewarm</html>"),
        FakeResponse(
            payload={"status_code": 0},
            cookies={"ttwid": "visitor-token"},
        ),
        FakeResponse(
            payload={
                "status_code": 0,
                "aweme_detail": {
                    "statistics": {"digg_count": 9, "comment_count": 2},
                },
            },
            text='{"status_code":0,"aweme_detail":{}}',
        ),
        FakeResponse(
            payload={
                "status_code": 0,
                "has_more": 0,
                "total": 2,
                "comments": [{"cid": "c1", "text": "访客评论"}],
            },
            text='{"status_code":0,"comments":[]}',
        ),
    )

    result = asyncio.run(EngagementCrawler(client=client).fetch(
        "https://www.douyin.com/video/7665718789363309172"
    ))

    assert result.coverage == "partial"
    assert result.stats.likes == 9
    assert result.comments[0].text == "访客评论"
    assert len(client.calls) == 4
    assert client.calls[1][0].endswith("/ttwid/union/register/")
    assert client.calls[2][1]["headers"]["Cookie"] == "ttwid=visitor-token"
    assert client.calls[2][1]["params"]["a_bogus"]


def test_kuaishou_protected_graphql_is_not_reported_as_target_data() -> None:
    client = FakeClient()
    result = asyncio.run(EngagementCrawler(client=client).fetch(
        "https://www.kuaishou.com/short-video/3xatjrjyuwrwzyk"
    ))

    assert result.coverage == "unsupported"
    assert "kww" in result.reason
    assert "Need captcha" in result.reason
    assert result.stats.likes is None
    assert result.comments == []
    assert client.calls == []


def test_comment_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="comment_limit"):
        asyncio.run(EngagementCrawler(client=FakeClient()).fetch("https://m.weibo.cn/detail/5301066679190033", comment_limit=0))


def test_comment_page_must_be_positive() -> None:
    with pytest.raises(ValueError, match="page"):
        asyncio.run(EngagementCrawler(client=FakeClient()).fetch_comments(
            "https://m.weibo.cn/detail/5301066679190033",
            "weibo",
            0,
        ))
