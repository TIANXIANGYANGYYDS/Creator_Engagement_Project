from __future__ import annotations

import asyncio
from typing import Any

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platform_session import PlatformSessionStore


class FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None, text: str = "", status_code: int = 200) -> None:
        self.payload = payload
        self.text = text
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        if self.payload is None:
            raise ValueError("no JSON")
        return self.payload


class DualFakeClient:
    def __init__(self, *, gets: list[FakeResponse] | None = None, posts: list[FakeResponse] | None = None) -> None:
        self.gets = list(gets or [])
        self.posts = list(posts or [])
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        return self.gets.pop(0)

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        return self.posts.pop(0)


def test_xiaohongshu_comments_sign_and_cursor_pagination() -> None:
    client = DualFakeClient(gets=[
        FakeResponse({
            "success": True,
            "data": {
                "comments": [{
                    "id": "x1",
                    "content": "第一页",
                    "user_info": {"nickname": "用户"},
                }],
                "cursor": "cursor-2",
                "has_more": True,
            },
        }),
        FakeResponse({
            "success": True,
            "data": {
                "comments": [{
                    "id": "x2",
                    "content": "第二页",
                    "user_info": {"nickname": "用户2"},
                }],
                "cursor": "",
                "has_more": False,
            },
        }),
    ])
    crawler = EngagementCrawler(
        client=client,
        platform_cookies={"xiaohongshu": "a1=a1-value; web_session=session-value"},
    )

    result = asyncio.run(crawler.fetch_comments(
        "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac?xsec_token=token",
        "小红书",
        2,
    ))

    assert result.comments[0].comment_id == "x2"
    assert result.next_page is None
    assert len(client.get_calls) == 2
    assert client.get_calls[0][0].startswith("https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?")
    assert client.get_calls[0][1]["headers"]["X-S"].startswith("XYS_")
    assert "cursor=" in client.get_calls[1][0]
    assert "cursor-2" in client.get_calls[1][0]


def test_kuaishou_detail_and_rest_comments_validate_target_id() -> None:
    client = DualFakeClient(posts=[
        FakeResponse({
            "data": {"visionVideoDetail": {"photo": {
                "id": "photo-1",
                "viewCount": 100,
                "likeCount": 12,
                "commentCount": 2,
            }}},
        }),
        FakeResponse({
            "result": 1,
            "commentCountV2": 2,
            "pcursorV2": "no_more",
            "rootCommentsV2": [{
                "commentId": "k1",
                "authorName": "快手用户",
                "content": "快手评论",
                "likedCount": 3,
                "timestamp": 1700000000000,
            }],
        }),
    ])
    crawler = EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1; kww=short-lived"},
    )

    result = asyncio.run(crawler.fetch(
        "https://www.kuaishou.com/short-video/photo-1",
        comment_limit=20,
    ))

    assert result.stats.views == 100
    assert result.stats.likes == 12
    assert result.stats.comments == 2
    assert result.comments[0].text == "快手评论"
    assert result.next_cursor is None
    assert client.post_calls[1][0].endswith("/rest/v/photo/comment/list")


def test_kuaishou_detail_mismatch_is_not_success() -> None:
    client = DualFakeClient(posts=[FakeResponse({
        "data": {"visionVideoDetail": {"photo": {"id": "other"}}},
    })])
    crawler = EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1"},
    )

    result = asyncio.run(crawler.fetch_interactions(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
    ))

    assert result.coverage == "failed"
    assert "photoId" in result.reason


def test_wechat_disabled_comments_are_distinguished_from_missing_session() -> None:
    html = """
    <script>
      window.cgiDataNew = {show_comment: 0, comment_id: 10, bizuin: 'MzA=', mid: 20, idx: 1};
      window.appmsgstat = {read_num: 123, like_num: 7, comment_count: 0};
    </script>
    """
    result = asyncio.run(EngagementCrawler(
        client=DualFakeClient(gets=[FakeResponse(text=html)]),
    ).fetch_comments(
        "https://mp.weixin.qq.com/s/article-token",
        "公众号",
        1,
    ))

    assert result.coverage == "complete"
    assert "关闭评论" in result.reason
    assert result.total_comments is None


def test_wechat_aidata_stats_do_not_need_wechat_session() -> None:
    client = DualFakeClient(gets=[
        FakeResponse(text="<html>article</html>"),
        FakeResponse({
            "data": {
                "stats": {
                    "read_count": 1200,
                    "like_count": 34,
                    "comment_count": 5,
                    "share_count": 6,
                    "collect_count": 7,
                }
            }
        }),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        aidata_api_key="provider-key",
    ).fetch_interactions(
        "https://mp.weixin.qq.com/s/article-token",
        "公众号",
    ))

    assert result.stats.views == 1200
    assert result.stats.likes == 34
    assert result.stats.comments == 5
    assert result.stats.favorites == 7
    assert result.source == "AIDATA weixin/mp/article/stats"
    assert client.get_calls[1][1]["headers"]["Authorization"] == "Bearer provider-key"


def test_wechat_aidata_comments_walk_buffer_pages() -> None:
    article = "<script>window.cgiDataNew={show_comment:1};</script>"
    client = DualFakeClient(gets=[
        FakeResponse(text=article),
        FakeResponse({"data": {
            "comments": [{"content_id": "w1", "content": "第一页"}],
            "buffer": "buffer-2",
            "has_more": True,
            "total_count": 2,
        }}),
        FakeResponse({"data": {
            "comments": [{
                "content_id": "w2",
                "content": "第二页",
                "created_at": 1700000000,
                "liked_count": 8,
                "reply_count": 3,
                "user": {"nickname": "微信用户"},
            }],
            "buffer": "",
            "has_more": False,
            "total_count": 2,
        }}),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        aidata_api_key="provider-key",
    ).fetch_comments(
        "https://mp.weixin.qq.com/s/article-token",
        "公众号",
        2,
    ))

    assert result.comments[0].comment_id == "w2"
    assert result.comments[0].author == "微信用户"
    assert result.comments[0].likes == 8
    assert result.total_comments == 2
    assert result.next_page is None
    assert client.get_calls[2][1]["params"]["buffer"] == "buffer-2"


def test_xiaohongshu_aidata_comments_walk_cursor_without_platform_login() -> None:
    client = DualFakeClient(gets=[
        FakeResponse({"data": {
            "comments": [{"id": "x1", "content": "第一页"}],
            "cursor": "cursor-2",
            "has_more": True,
        }}),
        FakeResponse({"data": {
            "comments": [{
                "id": "x2",
                "content": "第二页",
                "author": {"nickname": "小红书用户"},
                "stats": {"like_count": 9, "sub_comment_count": 4},
            }],
            "cursor": "",
            "has_more": False,
        }}),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        aidata_api_key="provider-key",
    ).fetch_comments(
        "https://www.xiaohongshu.com/explore/6a5a69260000000011011b41",
        "小红书",
        2,
    ))

    assert result.comments[0].comment_id == "x2"
    assert result.comments[0].author == "小红书用户"
    assert result.comments[0].likes == 9
    assert result.comments[0].replies == 4
    assert result.next_page is None
    assert client.get_calls[1][1]["params"]["cursor"] == "cursor-2"


def test_wechat_no_session_is_blocked_after_article_metadata() -> None:
    html = """
    <script>
      window.cgiDataNew = {show_comment: 1, comment_id: 10, bizuin: 'MzA=', mid: 20, idx: 1};
    </script>
    """
    client = DualFakeClient(gets=[
        FakeResponse(text=html),
        FakeResponse({"base_resp": {"ret": -3, "errmsg": "no session"}}),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"wechat": "wap_sid2=session"},
    ).fetch_comments(
        "https://mp.weixin.qq.com/s/article-token",
        "公众号",
        1,
    ))

    assert result.coverage == "blocked"
    assert "no session" in result.reason
    assert client.get_calls[1][0].endswith("/mp/appmsg_comment")


def test_platform_session_store_reads_playwright_state(tmp_path) -> None:
    path = tmp_path / "xiaohongshu.json"
    path.write_text(
        '{"cookies":[{"name":"a1","value":"abc"},{"name":"web_session","value":"xyz"}]}',
        encoding="utf-8",
    )
    store = PlatformSessionStore(tmp_path)
    assert store.cookie_header("xiaohongshu") == "a1=abc; web_session=xyz"


def test_xiaohongshu_invalid_ssr_page_is_not_reported_as_partial_success() -> None:
    result = asyncio.run(EngagementCrawler(
        client=DualFakeClient(gets=[FakeResponse(text="<html>404</html>")]),
    ).fetch_interactions(
        "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac",
        "小红书",
    ))

    assert result.coverage == "unsupported"
    assert result.stats.likes is None
