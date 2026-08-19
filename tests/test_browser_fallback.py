from __future__ import annotations

import asyncio

from app.crawlers.browser_fallback import _parse_douyin, _parse_xhs
from app.crawlers.engagement import EngagementCrawler
from app.models.engagement import EngagementStats


class FakeClient:
    async def get(self, url, *, params=None, headers=None):
        class Response:
            status_code = 200
            text = ""

            def json(self):
                return {}

        return Response()


class FakeBrowserFallback:
    def __init__(self):
        self.calls = []

    async def fetch(self, url, platform, work_id, **kwargs):
        self.calls.append((url, platform, work_id, kwargs))
        from app.models.engagement import EngagementResult

        return EngagementResult(
            platform=platform,
            canonical_url=url,
            work_id=work_id,
            coverage="partial",
            source="browser:test",
            stats=EngagementStats(likes=42),
        )


def test_protocol_unsupported_result_uses_browser_fallback() -> None:
    browser = FakeBrowserFallback()
    result = asyncio.run(
        EngagementCrawler(client=FakeClient(), browser_fallback=browser).fetch_interactions(
            "https://mp.weixin.qq.com/s/XKB0QLWfxHAJrOo-QvHsVw", "wechat"
        )
    )

    assert result.source == "browser:test"
    assert result.stats.likes == 42
    assert browser.calls[0][1:] == (
        "wechat",
        "XKB0QLWfxHAJrOo-QvHsVw",
        {
            "page": 1,
            "limit": 20,
            "include_stats": True,
            "include_comments": False,
        },
    )


def test_browser_parser_accepts_real_douyin_detail_shape() -> None:
    stats, comments, total, source = _parse_douyin(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/",
        {
            "aweme_detail": {
                "aweme_id": "1",
                "statistics": {
                    "play_count": 101,
                    "digg_count": 11,
                    "comment_count": 3,
                    "share_count": 5,
                    "collect_count": 7,
                },
            }
        },
        "1",
        EngagementStats(),
        [],
    )

    assert stats.likes == 11
    assert stats.comments == 3
    assert total == 3
    assert comments == []
    assert source == "aweme/detail"


def test_browser_parser_accepts_xhs_ssr_counters() -> None:
    stats, comments, total, source = _parse_xhs(
        "https://www.xiaohongshu.com/explore/1",
        'window.__INITIAL_STATE__={"likedCount":12,"collectedCount":4,"shareCount":2,"commentCount":8}',
        None,
        EngagementStats(),
        [],
    )

    assert stats.likes == 12
    assert stats.favorites == 4
    assert stats.shares == 2
    assert stats.comments == 8
    assert total == 8
    assert source == "note SSR"
