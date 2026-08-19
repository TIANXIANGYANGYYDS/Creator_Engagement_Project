from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.crawlers.engagement import (
    EngagementCrawler,
    _parse_xhs_stats,
    identify_url,
)


class FakeResponse:
    def __init__(self, *, payload: dict[str, Any] | None = None, text: str = "", status_code: int = 200) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class FakeClient:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.bilibili.com/video/BV1abc123", ("bilibili", "BV1abc123")),
        ("https://www.bilibili.com/video/av12345", ("bilibili", "12345")),
        ("https://m.weibo.cn/detail/5301066679190033", ("weibo", "5301066679190033")),
        ("https://www.xiaohongshu.com/explore/6a5585c000000000080326ac", ("xiaohongshu", "6a5585c000000000080326ac")),
        ("https://haokan.baidu.com/v?vid=327646248367276281", ("haokan", "327646248367276281")),
        ("https://www.douyin.com/video/7665718789363309172", ("douyin", "7665718789363309172")),
        ("https://www.toutiao.com/article/1234567890/", ("toutiao", "1234567890")),
        ("https://www.kuaishou.com/short-video/abc", ("kuaishou", "abc")),
        ("https://mp.weixin.qq.com/s?mid=2247504578", ("wechat", "2247504578")),
    ],
)
def test_identify_url(url: str, expected: tuple[str, str]) -> None:
    assert identify_url(url) == expected


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
                "page": {"num": 1, "size": 20, "count": 21},
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


def test_toutiao_fetches_comment_count_and_comments_without_signature() -> None:
    client = FakeClient(FakeResponse(payload={
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
    }))
    result = asyncio.run(EngagementCrawler(client=client).fetch(
        "https://www.toutiao.com/article/7557632662635840036/"
    ))

    assert result.coverage == "partial"
    assert result.stats.comments == 28
    assert result.comments[0].author == "头条用户"
    assert result.comments[0].text == "头条评论"
    assert result.next_cursor == "5"
    assert "_signature" not in client.calls[0][1]["params"]


def test_haokan_comments_do_not_require_captured_signature() -> None:
    client = FakeClient(FakeResponse(payload={
        "status": 0,
        "data": {
            "comment_count": "13",
            "is_over": False,
            "list": [{"reply_id": "r1", "uname": "用户", "content": "内容", "like_count": "4", "reply_count": "2"}],
        },
    }))
    result = asyncio.run(EngagementCrawler(client=client).fetch("https://haokan.baidu.com/v?vid=327646248367276281"))

    assert result.stats.comments == 13
    assert result.comments[0].comment_id == "r1"
    assert "hk_sign" not in client.calls[0][1]["params"]


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
    assert _parse_xhs_stats("<html></html>", "missing").model_dump() == {
        "views": None,
        "likes": None,
        "comments": None,
        "shares": None,
        "favorites": None,
        "coins": None,
        "danmaku": None,
        "reposts": None,
    }


def test_xiaohongshu_blocked_page_is_not_reported_as_partial_success() -> None:
    result = asyncio.run(EngagementCrawler(client=FakeClient(
        FakeResponse(text="challenge", status_code=403),
    )).fetch(
        "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac"
    ))

    assert result.coverage == "blocked"
    assert result.stats.likes is None


def test_session_bound_platform_is_explicitly_unsupported() -> None:
    result = asyncio.run(EngagementCrawler(client=FakeClient()).fetch("https://www.douyin.com/video/7665718789363309172"))
    assert result.coverage == "unsupported"
    assert "a_bogus" in result.reason


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
