from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.crawlers.engagement import EngagementCrawler
from app.api.routers.engagement import _fetch_all_comments_with_status
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.bilibili import OPUS_DETAIL_URL, parse_comments
from app.crawlers.platforms.bilibili import DYNAMIC_DETAIL_URL


def response(payload=None, *, status=200, text=""):
    return SimpleNamespace(status_code=status, text=text or json.dumps(payload), json=lambda: payload)


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    post = get


class NoBrowserExpected:
    async def fetch(self, *args, **kwargs):
        pytest.fail("An explicitly unavailable target must not launch a browser")


def dynamic_fallback_client(payload, *, fallback=None):
    return Client(
        response(text='window.__INITIAL_STATE__={"detail":null};'),
        response({"code": 0, "data": {"b_3": "guest-device"}}),
        response({"code": 0, "data": {"item": None, "fallback":
            fallback if fallback is not None else {"type": 1, "id": "123"}}}),
        response(payload),
    )


def test_bilibili_dynamic_desktop_recovers_stats_on_empty_ssr():
    client = dynamic_fallback_client({"code": 0, "data": {"item": {
        "id_str": "123", "modules": [{"module_stat": {
            "like": {"count": 0}, "comment": {"count": 2},
        }}], "orig": {"modules": [{"module_stat": {"like": {"count": 999}}}]},
    }}})
    crawler = EngagementCrawler(client=client, max_protocol_attempts=1,
                                browser_fallback=NoBrowserExpected())
    result = asyncio.run(crawler.fetch_interactions("https://t.bilibili.com/123", "B站"))
    assert result.coverage == "partial" and result.work_id == "123"
    assert result.stats.likes == 0 and result.stats.comments == 2
    assert result.stats.views is None and result.stats.shares is None
    assert client.calls[3][0] == DYNAMIC_DETAIL_URL
    assert client.calls[3][1]["headers"]["Cookie"] == "buvid3=guest-device"
    assert client.calls[3][1]["params"]["id"] == "123"


def test_bilibili_dynamic_comment_thread_comes_from_target_stat_module():
    client = dynamic_fallback_client({"code": 0, "data": {"item": {
        "id_str": "123", "basic": {"rid_str": "456", "rtype": 8},
        "modules": [{"module_stat": {"comment": {
            "comment_id": "456", "comment_type": 1, "count": 1,
        }}}],
    }}})
    client.responses.extend([
        response({"code": -500}),
        response({"code": 0, "data": {"page": {"num": 1, "size": 20, "count": 1},
            "replies": [{"rpid_str": "789", "oid": 456, "root": 0,
                "content": {"message": "root comment"}, "member": {"uname": "guest"},
                "rcount": 3, "replies": [{"content": {"message": "nested reply"}}]}]}}),
    ])
    crawler = EngagementCrawler(client=client, max_protocol_attempts=1,
                                browser_fallback=NoBrowserExpected())
    result = asyncio.run(crawler.fetch_comments("https://t.bilibili.com/123", "B站", 1))
    assert result.coverage == "partial"
    assert [c.text for c in result.comments] == ["root comment"]
    assert result.comments[0].replies == 3
    assert client.calls[-1][1]["params"]["oid"] == "456"
    assert client.calls[-1][1]["params"]["type"] == 1


@pytest.mark.parametrize("fallback", [{"type": 1, "id": "999"}, {"type": 2, "id": "123"}])
def test_bilibili_dynamic_rejects_unverified_fallback(fallback):
    client = dynamic_fallback_client({}, fallback=fallback)
    result = asyncio.run(EngagementCrawler(client=client, max_protocol_attempts=1).fetch_interactions(
        "https://t.bilibili.com/123", "B站"))
    assert result.coverage == "failed" and result.stats.likes is None
    assert len(client.calls) == 3


@pytest.mark.parametrize("payload, coverage", [
    ({"code": -352}, "blocked"),
    ({"code": 4101152}, "unsupported"),
    ({"code": 0, "data": {"item": {"id_str": "999"}}}, "failed"),
    ({"code": 0, "data": {"item": None}}, "failed"),
])
def test_bilibili_dynamic_rejects_blocked_unavailable_or_wrong_target(payload, coverage):
    client = dynamic_fallback_client(payload)
    result = asyncio.run(EngagementCrawler(client=client, max_protocol_attempts=1).fetch_interactions(
        "https://t.bilibili.com/123", "B站"))
    assert result.coverage == coverage and result.stats.likes is None


def weibo_client(*comments, count=0, mid="5336588076712748"):
    return Client(
        response(text='<script>var request_id = "0123456789abcdef";</script>'),
        response(text='visitor_gray_callback({"retcode":20000000,"data":{"sub":"guest"}});'),
        response({"idstr": mid, "mblogid": "RflP1sai8", "comments_count": count}),
        *(response(payload) for payload in comments),
    )


@pytest.mark.parametrize("count", [0, 3, None])
def test_weibo_explicit_empty_messages_preserve_known_total(count):
    client = weibo_client(
        {"ok": 0, "msg": "还没有人评论哦~快来抢沙发！"},
        {"ok": 0, "msg": "暂无数据"}, count=count,
    )
    result = asyncio.run(EngagementCrawler(client=client, max_protocol_attempts=3).fetch_comments(
        "https://weibo.com/6660086860/RflP1sai8", "微博", 1,
    ))
    assert result.coverage == "partial"
    assert result.comments == [] and result.next_page is None
    assert result.total_comments == count
    assert result.protocol_attempts == 1 and len(client.calls) == 5


def test_weibo_empty_message_does_not_hide_login_rejection():
    client = weibo_client(
        {"ok": 0, "msg": "还没有人评论哦~快来抢沙发！"},
        {"ok": 0, "msg": "请先登录"},
    )
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://weibo.com/6660086860/RflP1sai8", "微博", 1,
    ))
    assert result.coverage == "blocked"
    assert "请先登录" in result.reason
    assert result.total_comments is None


def test_weibo_wrong_target_cannot_confirm_zero_comments():
    client = weibo_client(mid="999")
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://weibo.com/6660086860/RflP1sai8", "微博", 1,
    ))
    assert result.coverage == "failed" and "MID" in result.reason
    assert result.total_comments is None
    assert len(client.calls) == 3


@pytest.mark.parametrize("payload", [{}, {"ok": 1}, {"ok": 1, "data": []}, {"ok": 1, "data": {}}])
def test_weibo_invalid_payload_is_not_empty_success(payload):
    client = weibo_client(payload, payload)
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://weibo.com/6660086860/RflP1sai8", "微博", 1,
    ))
    assert result.coverage == "blocked"


def test_weibo_hotflow_deep_page_cannot_switch_to_numbered_stream():
    client = weibo_client(
        {"ok": 1, "data": {"data": [{"id": "first"}], "max_id": "next", "max_id_type": 0}},
        {"ok": -100}, count=48,
    )
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://weibo.com/6660086860/RflP1sai8", "微博", 2,
    ))
    assert result.coverage == "blocked" and "ok=-100" in result.reason
    assert not any(url.endswith("/api/comments/show") for url, _ in client.calls)


def test_weibo_numbered_fallback_resumes_the_same_stream():
    client = weibo_client(
        {"ok": -100},
        {"ok": 1, "data": {"data": [{"id": "first"}], "max": 2, "total_number": 11}},
        count=11,
    )
    client.responses.extend([
        response({"idstr": "5336588076712748", "mblogid": "RflP1sai8", "comments_count": 11}),
        response({"ok": 1, "data": {"data": [{"id": "second"}], "max": 2, "total_number": 11}}),
    ])

    async def run():
        crawler = EngagementCrawler(client=client)
        url = "https://weibo.com/6660086860/RflP1sai8"
        comments, total, complete = await _fetch_all_comments_with_status(
            SimpleNamespace(fetch_comments=crawler.fetch_comments), url, "微博",
        )
        assert [comment.comment_id for comment in comments] == ["first", "second"]
        assert total == 11 and complete is False

    asyncio.run(run())
    assert sum(url.endswith("/comments/hotflow") for url, _ in client.calls) == 1
    assert client.calls[-1][1]["params"]["page"] == 2


def test_weibo_hotflow_resume_cannot_switch_stream_after_retry():
    client = weibo_client({"ok": -100}, count=48)
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://weibo.com/6660086860/RflP1sai8", "微博", 2, cursor="previous-hotflow-cursor",
    ))
    assert result.coverage == "blocked" and "ok=-100" in result.reason
    assert not any(url.endswith("/api/comments/show") for url, _ in client.calls)


@pytest.mark.parametrize("operation", ["comments", "interactions"])
@pytest.mark.parametrize("code,reason", [(62012, "仅 UP 主自己可见"), (62002, "稿件不可见")])
def test_bilibili_private_video_stops_retries_and_browser(operation, code, reason):
    client = Client(response({"code": code, "message": reason}))
    crawler = EngagementCrawler(client=client, max_protocol_attempts=3,
                                browser_fallback=NoBrowserExpected())
    fetch = crawler.fetch_comments if operation == "comments" else crawler.fetch_interactions
    args = (1,) if operation == "comments" else ()
    result = asyncio.run(fetch("https://www.bilibili.com/video/BV1Vnxi6dEts", "B站", *args))
    assert result.coverage == "unsupported"
    assert str(code) in result.reason and reason in result.reason
    assert result.protocol_attempts == 1 and len(client.calls) == 1


@pytest.mark.parametrize("page_response", [response(status=404), response(text="<title>验证码</title>")])
def test_bilibili_opus_confirms_unavailability_with_api(page_response):
    client = Client(
        page_response,
        response({"code": 0, "data": {"b_3": "current-device", "b_4": "current-device4"}}),
        response({"code": 4101152, "message": "动态不可见"}),
    )
    crawler = EngagementCrawler(client=client, max_protocol_attempts=3,
                                browser_fallback=NoBrowserExpected())
    result = asyncio.run(crawler.fetch_comments(
        "https://www.bilibili.com/opus/1265026696616083496", "B站", 1,
    ))
    assert result.coverage == "unsupported" and "4101152" in result.reason
    assert result.total_comments is None
    assert result.protocol_attempts == 1 and len(client.calls) == 3
    assert client.calls[2][0] == OPUS_DETAIL_URL
    assert client.calls[2][1]["params"]["id"] == "1265026696616083496"
    assert client.calls[2][1]["headers"]["Cookie"] == "buvid3=current-device; buvid4=current-device4"


def test_bilibili_opus_missing_page_can_recover_public_comments():
    client = Client(
        response(status=404),
        response({"code": 0, "data": {"b_3": "guest"}}),
        response({"code": 0, "data": {"item": {
            "id_str": "123", "basic": {"comment_id_str": "456", "comment_type": 12},
        }}}),
        response({"code": -500}),  # WBI unavailable: keep the existing legacy fallback.
        response({"code": 0, "data": {
            "page": {"num": 1, "size": 20, "count": 1},
            "replies": [{"rpid": 789, "rcount": 2, "content": {"message": "public root"}}],
        }}),
    )
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://www.bilibili.com/opus/123", "B站", 1,
    ))
    assert result.coverage == "partial" and result.work_id == "123"
    assert result.comments[0].text == "public root" and result.comments[0].replies == 2
    assert client.calls[-1][1]["params"]["oid"] == "456"
    assert client.calls[-1][1]["params"]["type"] == 12


@pytest.mark.parametrize("code", [-352, -500, 12345])
def test_bilibili_opus_other_failures_remain_retryable(code):
    client = Client(response(status=404), response({"code": 0, "data": {"b_3": "guest"}}),
                    response({"code": code}))
    crawler = EngagementCrawler(client=client)
    result = asyncio.run(crawler.fetch_comments("https://www.bilibili.com/opus/123", "B站", 1))
    assert result.coverage in {"blocked", "failed"}
    assert str(code) in result.reason
    assert crawler._should_use_browser_fallback(result, include_stats=False, include_comments=True)


def test_bilibili_opus_api_must_match_requested_target():
    client = Client(response(status=404), response({"code": 0, "data": {"b_3": "guest"}}),
                    response({"code": 0, "data": {"item": {"id_str": "999"}}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://www.bilibili.com/opus/123", "B站", 1,
    ))
    assert result.coverage == "failed" and "matching detail" in result.reason
    assert result.total_comments is None


def test_bilibili_opus_keeps_verified_zero_when_reply_api_omits_total():
    client = Client(
        response(text='<script>window.__INITIAL_STATE__=' + json.dumps({"detail": {
            "id_str": "123", "basic": {"comment_id_str": "456", "comment_type": 12},
            "modules": [{"module_stat": {"comment": {"count": 0}}}],
        }}) + ';</script>'),
        response({"code": 0, "data": {"wbi_img": {
            "img_url": "https://i0.hdslb.com/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/" + "b" * 32 + ".png",
        }}}),
        response({"code": 0, "data": {
            "cursor": {"is_begin": True, "is_end": True, "next": 0}, "replies": [],
        }}),
    )
    result = asyncio.run(EngagementCrawler(client=client, max_protocol_attempts=3).fetch_comments(
        "https://www.bilibili.com/opus/123", "B站", 1,
    ))
    assert result.coverage == "partial" and result.total_comments == 0
    assert result.comments == [] and result.next_page is None
    assert result.protocol_attempts == 1 and len(client.calls) == 3


@pytest.mark.parametrize("data", [None, {}, {"replies": {}}, {"replies": ""}])
def test_bilibili_invalid_comment_list_is_not_zero(data):
    with pytest.raises(PlatformCrawlerError, match="no comment list"):
        parse_comments({"code": 0, "data": data})
