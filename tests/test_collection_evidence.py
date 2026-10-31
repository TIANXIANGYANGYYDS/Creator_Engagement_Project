"""Regressions for October 30 target identity, missing counters and comment APIs."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from app.crawlers.browser_fallback import BrowserFallback, _parse_toutiao
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms import sohu, toutiao, xueqiu
from app.crawlers.platforms.bytedance_publishers import fetch as fetch_micro
from app.crawlers.platforms.douyin import public_view_count
from app.models.engagement import EngagementStats


def ssr(work_id, counts=None, **extra):
    data = {"groupId": work_id, "itemCell": {"itemCounter": counts or {}}, **extra}
    return '<script id="RENDER_DATA">' + quote(json.dumps({"data": data})) + '</script>'


def comment_payload(**extra):
    return {"err_no": 0, "group": {"group_id": "123", "user_id": 456},
            "total_number": 0, "data": [], "has_more": False, "offset": 20, **extra}


def collect(handler, crawler, *, comments=False, page=1):
    return asyncio.run(handler(crawler, "https://www.toutiao.com/article/123/", "123", 20,
        page=page, comment_cursor=None, include_stats=True, include_comments=comments))


@pytest.mark.parametrize("handler", [toutiao.fetch, fetch_micro])
def test_target_comment_count_survives_missing_ssr(handler):
    crawler = SimpleNamespace(_get_response=AsyncMock(return_value=SimpleNamespace(text="<html>challenge</html>")),
                              _get_json=AsyncMock(return_value=comment_payload()))
    result = collect(handler, crawler)
    assert result.coverage == "partial" and result.stats.comments == 0
    assert result.stats.views is None and result.stats.likes is None
    assert result.comments == []


@pytest.mark.parametrize("changes", [
    {"group": {"group_id": "999", "user_id": 456}},
    {"group": {"group_id": "123", "user_id": 0}},
    {"group": {}}, {"err_no": 1}, {"total_number": None},
    {"has_more": True},
])
def test_comment_error_or_echoed_id_is_never_a_successful_zero(changes):
    with pytest.raises(PlatformCrawlerError):
        toutiao.parse_comment_page(comment_payload(**changes), "123", 0)


def test_comment_cursor_is_used_and_must_advance():
    payload = comment_payload(total_number=3, has_more=True, offset=40,
        data=[{"comment": {"id_str": "456", "text": "root", "reply_count": 1}}])
    crawler = SimpleNamespace(_get_json=AsyncMock(return_value=payload))
    comments, total, following = asyncio.run(toutiao.fetch_comment_page(
        crawler, "123", 20, page=2, comment_cursor="25"))
    assert crawler._get_json.call_args.kwargs["params"]["offset"] == "25"
    assert crawler._get_json.call_args.kwargs["params"]["tab_index"] == 1
    assert (total, following, comments[0].replies) == (3, "40", 1)
    for offset in (40, 50):
        with pytest.raises(PlatformCrawlerError):
            toutiao.parse_comment_page(payload, "123", offset)


def test_ssr_never_borrows_counters_from_reposts_or_recommendations():
    text = ssr("123", None, likeData={"count": 0},
               originThread={"itemCounter": {"readCount": 999999}},
               detailFeedList=[{"itemCounter": {"readCount": 888888}}])
    stats = toutiao.parse_stats(text, "123")
    assert stats.views is None and stats.likes == 0
    with pytest.raises(PlatformCrawlerError):
        toutiao.parse_stats(text, "999")
    assert toutiao.parse_stats('{"itemCounter":{"readCount":999}}', "123").views is None


def test_toutiao_zero_read_count_is_unknown_and_impressions_are_separate():
    stats = toutiao.parse_stats(ssr("123", {"readCount": 0, "showCount": 532, "commentCount": 0}), "123")
    assert stats.views is None and stats.model_extra["show_count"] == 532
    assert stats.comments == 0


def test_browser_ignores_another_articles_comments():
    old = EngagementStats(likes=3, comments=1)
    stats, comments, total, source = _parse_toutiao(
        "https://www.toutiao.com/article/v4/tab_comments/?group_id=999", "",
        comment_payload(group={"group_id": "999", "user_id": 456}), "123", old, [])
    assert stats is old and total == 1 and comments == [] and source == ""


def test_browser_uses_original_ssr_after_dom_hydration_removed_it():
    target = "https://www.toutiao.com/article/123/"
    page = SimpleNamespace(url=target, content=AsyncMock(return_value="<html/>"))
    response = SimpleNamespace(url=target, text=AsyncMock(return_value=ssr("123", {"commentCount": 6})))
    result = asyncio.run(BrowserFallback()._parse_responses(page, [response],
        "https://weitoutiao.zjurl.cn/ugc/share/wap/comment/999/", "toutiao", "999",
        page=1, limit=20, include_stats=True, include_comments=False))
    assert result.work_id == "123" and result.stats.comments == 6
    page.content.assert_not_called()


def sohu_crawler(*, source="mp_123", page=1):
    items = [{"id": 1, "topic_id": 7, "user_name": "reader", "content": "root",
              "reply_id": 0, "reply_count": 1, "like_count": 0, "date": "2026-10-30T19:00:21.713+08:00"},
             {"id": 2, "topic_id": 7, "content": "reply body", "reply_id": 1}]
    body = {"source_id": source, "topic_id": 7, "total_count": 21, "participation_sum": 22,
            "comments": items if page == 1 else []}
    return SimpleNamespace(_get_response=AsyncMock(side_effect=[
        SimpleNamespace(text='var __autoArticleConfig = {news_id:"123",media_id:"456"};'),
        SimpleNamespace(text="313144")]),
        _get_json=AsyncMock(return_value={"code": 200, "data": body}))


def test_sohu_auto_uses_current_comment_api_and_verified_pv():
    crawler = sohu_crawler()
    result = collect(sohu.fetch, crawler, comments=True)
    assert result.stats.views == 313144 and result.stats.comments == 22
    assert result.stats.likes is None and result.stats.favorites is None
    assert [c.text for c in result.comments] == ["root"]
    assert result.comments[0].replies == 1 and result.comments[0].created_at is not None
    assert result.next_cursor == "2"
    assert crawler._get_json.call_args.args[0] == "https://portal.auto.sohu.com/comment/list"


def test_sohu_auto_rejects_another_thread():
    result = collect(sohu.fetch, sohu_crawler(source="mp_999"), comments=True)
    assert result.coverage == "failed" and result.stats.comments is None


def test_sohu_auto_empty_page_does_not_claim_end_with_more_remaining():
    result = collect(sohu.fetch, sohu_crawler(page=2), comments=True)
    assert result.coverage == "failed"


def test_sohu_auto_page_past_total_has_no_next_page():
    result = collect(sohu.fetch, sohu_crawler(page=2), comments=True, page=3)
    assert result.coverage == "partial" and result.comments == [] and result.next_cursor is None


@pytest.mark.parametrize("raw,expected", [(0, None), (None, None), (62, 62)])
def test_hidden_zero_views_are_not_reported_as_measured_zero(raw, expected):
    assert public_view_count({"play_count": raw, "digg_count": 0}) == expected
    crawler = SimpleNamespace(_get_response=AsyncMock(),
        _get_json=AsyncMock(return_value={"id": "123", "view_count": raw, "like_count": 0}))
    result = collect(xueqiu.fetch, crawler)
    assert result.stats.views == expected and result.stats.likes == 0
