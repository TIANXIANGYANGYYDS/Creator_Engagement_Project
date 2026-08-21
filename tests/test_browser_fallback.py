from __future__ import annotations

import asyncio

from app.crawlers.browser_fallback import (
    BrowserFallback,
    BrowserFallbackSettings,
    _browser_target_url,
    _number,
    _parse_douyin,
    _parse_kuaishou_guest,
    _parse_toutiao,
    _parse_xhs,
)
from app.crawlers.engagement import EngagementCrawler
from app.models.engagement import EngagementResult, EngagementStats


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


class FakeBrowserContext:
    def __init__(self) -> None:
        self.cookies = []

    async def add_cookies(self, cookies) -> None:
        self.cookies.extend(cookies)


def test_browser_fallback_has_a_global_concurrency_limit() -> None:
    class CountingBrowserFallback(BrowserFallback):
        def __init__(self) -> None:
            super().__init__(settings=BrowserFallbackSettings(max_concurrency=2))
            self.active = 0
            self.max_active = 0

        async def _fetch_locked(self, url, platform, work_id, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return EngagementResult(
                platform=platform,
                canonical_url=url,
                work_id=work_id,
                coverage="partial",
            )

    async def run() -> CountingBrowserFallback:
        browser = CountingBrowserFallback()
        await asyncio.gather(
            browser.fetch("u1", "douyin", "1", page=1, limit=20, include_stats=True, include_comments=True),
            browser.fetch("u2", "kuaishou", "2", page=1, limit=20, include_stats=True, include_comments=True),
            browser.fetch("u3", "weibo", "3", page=1, limit=20, include_stats=True, include_comments=True),
        )
        return browser

    browser = asyncio.run(run())

    assert browser.max_active == 2


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


def test_caller_cookie_is_only_seeded_into_douyin_profile() -> None:
    browser = BrowserFallback(cookies="sessionid=caller-owned")
    kuaishou = FakeBrowserContext()
    douyin = FakeBrowserContext()

    asyncio.run(browser._seed_cookies(
        kuaishou,
        "https://www.kuaishou.com/short-video/1",
        "kuaishou",
    ))
    asyncio.run(browser._seed_cookies(
        douyin,
        "https://www.douyin.com/video/1",
        "douyin",
    ))

    assert kuaishou.cookies == []
    assert douyin.cookies == [{
        "name": "sessionid",
        "value": "caller-owned",
        "url": "https://www.douyin.com",
    }]


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


def test_browser_parser_does_not_treat_hidden_douyin_views_as_zero() -> None:
    stats, _, _, _ = _parse_douyin(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/",
        {
            "aweme_detail": {
                "aweme_id": "1",
                "statistics": {"play_count": 0, "digg_count": 11},
            }
        },
        "1",
        EngagementStats(),
        [],
    )

    assert stats.views is None
    assert stats.likes == 11


def test_browser_toutiao_stats_ignore_unrelated_document_response() -> None:
    target_stats, comments, _, source = _parse_toutiao(
        "https://www.toutiao.com/article/7557632662635840036/",
        'window.data={"itemCounter":{"readCount":3307,"commentCount":28}}',
        None,
        "7557632662635840036",
        EngagementStats(),
        [],
    )
    stats, _, _, unrelated_source = _parse_toutiao(
        "https://www.toutiao.com/article/7000000000000000000/",
        'window.data={"itemCounter":{"readCount":5,"commentCount":0}}',
        None,
        "7557632662635840036",
        target_stats,
        comments,
    )

    assert target_stats.views == 3307
    assert source == "article SSR"
    assert stats.views == 3307
    assert unrelated_source == ""


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


def test_kuaishou_guest_payload_matches_only_target_and_parses_comments() -> None:
    stats, comments, total, has_next, source = _parse_kuaishou_guest(
        {
            "apolloState": {
                "VisionVideoDetailPhoto:other": {"id": "other", "realLikeCount": 999},
            },
            "apolloPhoto": {
                "id": "photo-1",
                "viewCount": "3.9万",
                "realLikeCount": 11626,
            },
            "commentPage": {
                "result": 1,
                "commentCountV2": 202,
                "pcursorV2": "cursor-2",
                "rootCommentsV2": [{
                    "commentId": "comment-1",
                    "authorName": "游客可见用户",
                    "content": "游客评论",
                    "timestamp": 1700000000000,
                    "likedCount": 3,
                    "subCommentCount": 2,
                }],
            },
            "reached": True,
        },
        "photo-1",
    )

    assert stats.views == 39000
    assert stats.likes == 11626
    assert total == 202
    assert comments[0].author == "游客可见用户"
    assert comments[0].likes == 3
    assert comments[0].replies == 2
    assert has_next is True
    assert "guest" in source


def test_browser_number_accepts_chinese_display_units() -> None:
    assert _number("1.2万") == 12000
    assert _number("2.5亿") == 250000000


def test_browser_normalizes_share_and_desktop_content_urls() -> None:
    assert _browser_target_url(
        "https://c.kuaishou.com/fw/photo/3x4zebgce2jutx2",
        "kuaishou",
        "3x4zebgce2jutx2",
    ) == "https://www.kuaishou.com/short-video/3x4zebgce2jutx2"
    assert _browser_target_url(
        "http://weibo.com/6539142196/RdVPmEYKD",
        "weibo",
        "5333205569766651",
    ) == "https://m.weibo.cn/detail/5333205569766651"


def test_xhs_guest_does_not_mislabel_first_page_as_page_two() -> None:
    class Locator:
        async def inner_text(self, timeout=0):
            return "登录查看全部评论内容"

    class Page:
        def locator(self, selector):
            return Locator()

    class Response:
        url = "https://edith.xiaohongshu.com/api/sns/web/v2/comment/page"

        async def text(self):
            return '{"data":{"has_more":true,"comments":[{"id":"x1","content":"首屏"}]}}'

    result = asyncio.run(BrowserFallback()._parse_responses(
        Page(),
        [Response()],
        "https://www.xiaohongshu.com/explore/6a5a69260000000011011b41",
        "xiaohongshu",
        "6a5a69260000000011011b41",
        page=2,
        limit=20,
        include_stats=False,
        include_comments=True,
    ))

    assert result.coverage == "unsupported"
    assert result.comments == []
    assert result.next_cursor is None
    assert "游客态仅开放首屏" in result.reason


def test_browser_opens_comment_panel_before_scrolling_inner_container() -> None:
    events = []

    class Locator:
        @property
        def first(self):
            return self

        async def is_visible(self, timeout=0):
            return True

        async def click(self, timeout=0):
            events.append("click")

    class Page:
        def locator(self, selector):
            return Locator()

        async def evaluate(self, expression):
            events.append(expression)

        async def wait_for_timeout(self, timeout):
            pass

    asyncio.run(BrowserFallback()._interact_with_page(Page(), "xiaohongshu", 2))

    assert events[0] == "click"
    assert "overflowY" in events[1]
    assert len(events) == 5


def test_browser_stats_request_does_not_open_comment_panel() -> None:
    class Page:
        def locator(self, selector):
            raise AssertionError("interaction-only request must not locate comments")

        async def evaluate(self, expression):
            raise AssertionError("interaction-only request must not scroll comments")

    asyncio.run(
        BrowserFallback()._interact_with_page(
            Page(),
            "douyin",
            1,
            include_comments=False,
        )
    )
