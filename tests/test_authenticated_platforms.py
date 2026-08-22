from __future__ import annotations

import asyncio
import json
from typing import Any

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platform_session import PlatformSessionStore
from app.crawlers.platforms.wechat import parse_metadata as parse_wechat_metadata


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        text: str = "",
        status_code: int = 200,
        url: str = "",
    ) -> None:
        self.payload = payload
        self.text = text
        self.status_code = status_code
        self.url = url

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


def test_xiaohongshu_retries_blocked_xys_with_xyw() -> None:
    client = DualFakeClient(gets=[
        FakeResponse({"success": False, "code": -1}, status_code=406),
        FakeResponse({
            "success": True,
            "data": {
                "comments": [{
                    "id": "x1",
                    "content": "新签名返回",
                    "user_info": {"nickname": "用户"},
                }],
                "has_more": False,
            },
        }),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"xiaohongshu": "a1=a1-value; web_session=session-value"},
    ).fetch_comments(
        "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac?xsec_token=token",
        "小红书",
        1,
    ))

    assert result.comments[0].text == "新签名返回"
    assert client.get_calls[0][1]["headers"]["X-S"].startswith("XYS_")
    assert client.get_calls[1][1]["headers"]["X-S"].startswith("XYW_")
    assert "X-Xray-Traceid" in client.get_calls[1][1]["headers"]


def test_xiaohongshu_empty_authenticated_detail_is_retryable_block() -> None:
    client = DualFakeClient(
        gets=[FakeResponse(text="<html></html>")],
    )
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"xiaohongshu": "a1=a1-value; web_session=session-value"},
    ).fetch_interactions(
        "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac?xsec_token=token",
        "小红书",
    ))

    assert result.coverage == "blocked"
    assert "xsec_token" in result.reason
    assert len(client.post_calls) == 0
    assert len(client.get_calls) == 1


def test_xiaohongshu_interactions_prefer_one_unsigned_ssr_request() -> None:
    note_id = "6a5585c000000000080326ac"
    text = (
        '<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"'
        + note_id
        + '":{"note":{"noteId":"'
        + note_id
        + '","interactInfo":{"likedCount":"1.9万",'
        '"collectedCount":"246","shareCount":"523",'
        '"commentCount":"168"}}}}}}</script>'
    )
    client = DualFakeClient(gets=[FakeResponse(text=text)])
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"xiaohongshu": "a1=a1-value; web_session=session-value"},
    ).fetch_interactions(
        f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=token",
        "小红书",
    ))

    assert result.stats.likes == 19000
    assert result.stats.comments == 168
    assert result.source == "note SSR noteDetailMap"
    assert len(client.get_calls) == 1
    assert "xsec_source=pc_feed" in client.get_calls[0][0]
    assert "Cookie" not in client.get_calls[0][1]["headers"]
    assert "Referer" not in client.get_calls[0][1]["headers"]
    assert client.post_calls == []


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


def test_kuaishou_detail_mismatch_returns_only_validated_comment_total() -> None:
    client = DualFakeClient(
        gets=[FakeResponse(text="<html></html>")],
        posts=[
            FakeResponse({
                "data": {"visionVideoDetail": {"photo": {"id": "other"}}},
            }),
            FakeResponse({
                "result": 1,
                "commentCountV2": 2,
                "pcursorV2": "no_more",
                "rootCommentsV2": [],
            }),
        ],
    )
    crawler = EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1"},
    )

    result = asyncio.run(crawler.fetch_interactions(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
    ))

    assert result.coverage == "partial"
    assert "photoId" in result.reason
    assert result.stats.comments == 2
    assert result.stats.likes is None


def test_kuaishou_unavailable_detail_does_not_waste_retries() -> None:
    client = DualFakeClient(posts=[
        FakeResponse({
            "data": {"visionVideoDetail": {"status": 1040, "photo": None}},
        }),
        FakeResponse({
            "result": 1,
            "commentCountV2": 3,
            "pcursorV2": "no_more",
            "rootCommentsV2": [],
        }),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1"},
        max_protocol_attempts=3,
        protocol_retry_base_seconds=0,
    ).fetch_interactions(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
    ))

    assert result.coverage == "partial"
    assert result.protocol_attempts == 1
    assert result.stats.comments == 3
    assert result.stats.views is None
    assert "status=1040" in result.reason
    assert len(client.post_calls) == 2


def test_kuaishou_zero_comment_total_is_valid_data() -> None:
    client = DualFakeClient(posts=[
        FakeResponse({
            "data": {"visionVideoDetail": {"status": 1, "photo": {
                "id": "photo-1",
                "viewCount": 10,
                "realLikeCount": 0,
            }}},
        }),
        FakeResponse({
            "result": 1,
            "commentCountV2": 0,
            "pcursorV2": "no_more",
            "rootCommentsV2": [],
        }),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1"},
    ).fetch_interactions(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
    ))

    assert result.stats.views == 10
    assert result.stats.likes == 0
    assert result.stats.comments == 0


def test_kuaishou_comments_fall_back_to_graphql_after_rest_challenge() -> None:
    client = DualFakeClient(posts=[
        FakeResponse({"result": 50, "message": "Need captcha"}),
        FakeResponse({
            "data": {"visionCommentList": {
                "commentCountV2": 1,
                "pcursorV2": "no_more",
                "rootCommentsV2": [{
                    "commentId": "comment-1",
                    "content": "GraphQL 评论",
                }],
            }},
        }),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1"},
    ).fetch_comments(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
        1,
    ))

    assert result.total_comments == 1
    assert result.comments[0].text == "GraphQL 评论"
    assert len(client.post_calls) == 2


def test_kuaishou_preserves_detail_when_comment_total_stays_blocked() -> None:
    posts: list[FakeResponse] = []
    for _ in range(3):
        posts.extend([
            FakeResponse({
                "data": {"visionVideoDetail": {"status": 1, "photo": {
                    "id": "photo-1",
                    "viewCount": 100,
                    "realLikeCount": 12,
                }}},
            }),
            FakeResponse({"result": 50, "message": "Need captcha"}),
            FakeResponse({"errors": [{"message": "Need captcha"}]}),
        ])
    client = DualFakeClient(posts=posts)
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1"},
        max_protocol_attempts=3,
        protocol_retry_base_seconds=0,
    ).fetch_interactions(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
    ))

    assert result.coverage == "partial"
    assert result.protocol_attempts == 3
    assert result.stats.views == 100
    assert result.stats.likes == 12
    assert result.stats.comments is None
    assert "保留已验证" in result.reason
    assert len(client.post_calls) == 9


def test_kuaishou_retries_when_comment_payload_omits_total() -> None:
    posts: list[FakeResponse] = []
    for attempt in range(2):
        posts.extend([
            FakeResponse({
                "data": {"visionVideoDetail": {"status": 1, "photo": {
                    "id": "photo-1",
                    "viewCount": 100,
                    "realLikeCount": 12,
                }}},
            }),
            FakeResponse({"result": 50}),
            FakeResponse({
                "data": {"visionCommentList": {
                    "pcursorV2": "no_more",
                    "rootCommentsV2": [],
                    **({"commentCountV2": 2} if attempt else {}),
                }},
            }),
        ])
    client = DualFakeClient(posts=posts)
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1"},
        max_protocol_attempts=3,
        protocol_retry_base_seconds=0,
    ).fetch_interactions(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
    ))

    assert result.protocol_attempts == 2
    assert result.stats.comments == 2
    assert len(client.post_calls) == 6


def test_kuaishou_detail_falls_back_to_target_validated_apollo_state() -> None:
    state = {
        "defaultClient": {
            'VisionVideoDetailPhoto:photo-1': {
                "id": "photo-1",
                "viewCount": "101",
                "realLikeCount": 12,
            },
        },
    }
    client = DualFakeClient(
        gets=[FakeResponse(text=(
            "<script>window.__APOLLO_STATE__="
            + json.dumps(state)
            + ";window.other=1</script>"
        ))],
        posts=[
            FakeResponse(),
            FakeResponse({
                "result": 1,
                "commentCountV2": 4,
                "pcursorV2": "no_more",
                "rootCommentsV2": [],
            }),
        ],
    )
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1"},
    ).fetch_interactions(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
    ))

    assert result.stats.views == 101
    assert result.stats.likes == 12
    assert result.stats.comments == 4
    assert result.source.startswith("page __APOLLO_STATE__")


def test_kuaishou_comment_pages_exclude_repeated_pinned_comment() -> None:
    client = DualFakeClient(posts=[
        FakeResponse({
            "result": 1,
            "commentCountV2": 3,
            "pcursorV2": "cursor-2",
            "rootCommentsV2": [
                {"commentId": "pinned", "content": "置顶评论"},
                {"commentId": "page-1", "content": "第一页"},
            ],
        }),
        FakeResponse({
            "result": 1,
            "commentCountV2": 3,
            "pcursorV2": "no_more",
            "rootCommentsV2": [
                {"commentId": "pinned", "content": "置顶评论"},
                {"commentId": "page-2", "content": "第二页"},
            ],
        }),
    ])
    result = asyncio.run(EngagementCrawler(
        client=client,
        platform_cookies={"kuaishou": "userId=user-1; kww=short-lived"},
    ).fetch_comments(
        "https://www.kuaishou.com/short-video/photo-1",
        "快手",
        2,
    ))

    assert [comment.comment_id for comment in result.comments] == ["page-2"]
    assert result.total_comments == 3
    assert result.next_page is None


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


def test_wechat_reads_preloaded_first_page_without_session() -> None:
    html = r'''
    <script>
      window.cgiDataNew = {show_comment: 1, comment_id: 10, bizuin: 'MzA=', mid: 20, idx: 1};
      var preload_comment_list = '{"elected_comment":[{"content_id":"c1","nick_name":"读者","content":"预载评论","like_num":3}]}';
      var preload_comment_total_cnt = 7;
    </script>
    '''
    result = asyncio.run(EngagementCrawler(
        client=DualFakeClient(gets=[FakeResponse(text=html)]),
    ).fetch_comments(
        "https://mp.weixin.qq.com/s/article-token",
        "公众号",
        1,
    ))

    assert result.coverage == "partial"
    assert result.comments[0].comment_id == "c1"
    assert result.comments[0].text == "预载评论"
    assert result.total_comments == 7
    assert result.next_page == 2


def test_wechat_reads_unquoted_zero_and_v2_stats() -> None:
    html = """
    <script>
      window.appmsgstat = {
        read_num_v2: 1234,
        like_num_v2: 0,
        comment_count: 8,
        share_count: 9
      };
    </script>
    """
    result = asyncio.run(EngagementCrawler(
        client=DualFakeClient(gets=[FakeResponse(text=html)]),
    ).fetch_interactions(
        "https://mp.weixin.qq.com/s?__biz=MzA=&mid=20&idx=1&sn=abc",
        "公众号",
    ))

    assert result.stats.views == 1234
    assert result.stats.likes == 0
    assert result.stats.comments == 8
    assert result.stats.shares == 9


def test_wechat_metadata_uses_full_article_query_when_html_omits_biz() -> None:
    metadata = parse_wechat_metadata(
        "<script>window.cgiDataNew = {show_comment: 1, comment_id: 10};</script>",
        "https://mp.weixin.qq.com/s?__biz=MzA=&mid=20&idx=2&sn=abc",
    )

    assert metadata["biz"] == "MzA="
    assert metadata["mid"] == "20"
    assert metadata["idx"] == "2"
    assert metadata["sn"] == "abc"


def test_wechat_captcha_redirect_is_reported_as_blocked() -> None:
    result = asyncio.run(EngagementCrawler(
        client=DualFakeClient(gets=[FakeResponse(
            text="<html></html>",
            url="https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=token",
        )]),
    ).fetch_interactions(
        "https://mp.weixin.qq.com/s/article-token",
        "公众号",
    ))

    assert result.coverage == "blocked"
    assert "验证码" in result.reason


def test_wechat_owned_article_uses_current_official_analytics_api() -> None:
    html = """
    <script>
      window.cgiDataNew = {show_comment: 1, comment_id: 10, bizuin: 'MzA=', mid: 20, idx: 1};
      var ct = 1761926400;
    </script>
    """
    client = DualFakeClient(
        gets=[FakeResponse(text=html)],
        posts=[
            FakeResponse({"access_token": "stable-token", "expires_in": 7200}),
            FakeResponse({
                "list": [{
                    "msgid": "20_1",
                    "detail_list": [{
                        "stat_date": "2025-11-30",
                        "read_user": 4123,
                        "like_user": 386,
                        "zaikan_user": 191,
                        "share_user": 366,
                        "comment_count": 33,
                        "collection_user": 233,
                    }],
                }],
                "is_delay": False,
            }),
        ],
    )

    result = asyncio.run(EngagementCrawler(
        client=client,
        wechat_mp_app_id="wx-app-id",
        wechat_mp_app_secret="app-secret",
    ).fetch_interactions(
        "https://mp.weixin.qq.com/s?__biz=MzA=&mid=20&idx=1&sn=abc",
        "公众号",
    ))

    assert result.source.endswith("/datacube/getarticletotaldetail")
    assert result.stats.views == 4123
    assert result.stats.likes == 386
    assert result.stats.recommendations == 191
    assert result.stats.shares == 366
    assert result.stats.comments == 33
    assert result.stats.favorites == 233
    assert client.post_calls[0][0].endswith("/cgi-bin/stable_token")
    assert client.post_calls[1][1]["json"] == {
        "begin_date": "2025-11-01",
        "end_date": "2025-11-01",
    }


def test_wechat_owned_article_uses_official_comment_pagination() -> None:
    html = """
    <script>
      window.cgiDataNew = {show_comment: 1, comment_id: 10, bizuin: 'MzA=', mid: 20, idx: 2};
    </script>
    """
    client = DualFakeClient(
        gets=[FakeResponse(text=html)],
        posts=[
            FakeResponse({"access_token": "stable-token", "expires_in": 7200}),
            FakeResponse({
                "errcode": 0,
                "errmsg": "ok",
                "total": 25,
                "comment": [{
                    "user_comment_id": 998,
                    "openid": "reader-openid",
                    "content": "官方留言正文",
                    "create_time": 1761926400,
                    "reply": {"content": "作者回复", "create_time": 1761926500},
                }],
            }),
        ],
    )

    result = asyncio.run(EngagementCrawler(
        client=client,
        wechat_mp_app_id="wx-app-id",
        wechat_mp_app_secret="app-secret",
    ).fetch_comments(
        "https://mp.weixin.qq.com/s?__biz=MzA=&mid=20&idx=2&sn=abc",
        "公众号",
        2,
    ))

    assert result.coverage == "complete"
    assert result.total_comments == 25
    assert result.next_page == 3
    assert result.comments[0].comment_id == "998"
    assert result.comments[0].author == "reader-openid"
    assert result.comments[0].replies == 1
    assert client.post_calls[1][1]["json"] == {
        "msg_data_id": 20,
        "index": 1,
        "begin": 20,
        "count": 20,
        "type": 0,
    }


def test_wechat_official_api_refreshes_an_expired_supplied_token() -> None:
    html = """
    <script>
      window.cgiDataNew = {show_comment: 1, comment_id: 10, bizuin: 'MzA=', mid: 20, idx: 1};
      var ct = 1761926400;
    </script>
    """
    client = DualFakeClient(
        gets=[FakeResponse(text=html)],
        posts=[
            FakeResponse({"errcode": 40014, "errmsg": "invalid access_token"}),
            FakeResponse({"access_token": "refreshed-token", "expires_in": 7200}),
            FakeResponse({
                "list": [{
                    "msgid": "20_1",
                    "detail_list": [{"stat_date": "2025-11-01", "read_user": 99}],
                }],
            }),
        ],
    )

    result = asyncio.run(EngagementCrawler(
        client=client,
        wechat_mp_app_id="wx-app-id",
        wechat_mp_app_secret="app-secret",
        wechat_mp_access_token="expired-token",
    ).fetch_interactions(
        "https://mp.weixin.qq.com/s?__biz=MzA=&mid=20&idx=1&sn=abc",
        "公众号",
    ))

    assert result.stats.views == 99
    assert client.post_calls[0][1]["params"]["access_token"] == "expired-token"
    assert client.post_calls[1][0].endswith("/cgi-bin/stable_token")
    assert client.post_calls[2][1]["params"]["access_token"] == "refreshed-token"


def test_weibo_falls_back_to_anonymous_numbered_page() -> None:
    client = DualFakeClient(gets=[
        FakeResponse({
            "ok": 1,
            "data": {"max_id": 99, "data": [{"id": 1, "text": "热门首屏"}]},
        }),
        FakeResponse({
            "ok": -100,
            "url": "https://passport.weibo.com/sso/signin",
        }),
        FakeResponse({
            "ok": 1,
            "data": {
                "max": 34,
                "total_number": 336,
                "data": [{
                    "id": 2,
                    "text": '<span><img alt="[手指比心]"></span>',
                    "user": {"screen_name": "访客"},
                }],
            },
        }),
    ])
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://m.weibo.cn/detail/5301066679190033",
        "微博",
        2,
    ))

    assert result.comments[0].text == "[手指比心]"
    assert result.total_comments == 336
    assert result.next_page == 3
    assert client.get_calls[2][0].endswith("/api/comments/show")
    assert client.get_calls[2][1]["params"]["page"] == 2


def test_platform_session_store_reads_playwright_state(tmp_path) -> None:
    path = tmp_path / "xiaohongshu.json"
    path.write_text(
        '{"cookies":[{"name":"a1","value":"abc"},{"name":"web_session","value":"xyz"}]}',
        encoding="utf-8",
    )
    store = PlatformSessionStore(tmp_path)
    assert store.cookie_header("xiaohongshu") == "a1=abc; web_session=xyz"


def test_platform_session_store_saves_private_playwright_state(tmp_path) -> None:
    class FakeContext:
        async def storage_state(self, *, path: str) -> None:
            from pathlib import Path

            Path(path).write_text('{"cookies":[]}', encoding="utf-8")

    store = PlatformSessionStore(tmp_path)
    path = asyncio.run(store.save_context("wechat", FakeContext()))

    assert path == tmp_path / "wechat.json"
    assert path.stat().st_mode & 0o777 == 0o600


def test_xiaohongshu_invalid_ssr_page_is_not_reported_as_partial_success() -> None:
    result = asyncio.run(EngagementCrawler(
        client=DualFakeClient(gets=[FakeResponse(text="<html>404</html>")]),
    ).fetch_interactions(
        "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac",
        "小红书",
    ))

    assert result.coverage == "unsupported"
    assert result.stats.likes is None
