from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.crawlers.authors import bilibili, kuaishou, toutiao, weibo, xiaohongshu
from app.crawlers.authors.common import AuthorSessionError, decode_cursor
from app.crawlers.authors.session import AuthorCrawler
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platform_session import PlatformSessionStore
from app.models.author import AuthorRequest, AuthorWork
from app.services.author_service import AuthorService
from app.services.engagement_service import EngagementService


class Replies:
    def __init__(self, *values, cookie=""):
        self.values = list(values)
        self.calls = []
        self.cookie = cookie

    def _platform_cookie(self, platform):
        return self.cookie

    def cookie_for_url(self, url):
        return self.cookie

    async def _get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    _post_json = _get_json

    async def _get_response(self, url, **kwargs):
        return SimpleNamespace(text=await self._get_json(url, **kwargs))

    async def _ensure_weibo_visitor_session(self, *args, **kwargs):
        pass

    def _weibo_visitor_headers(self):
        return {}


def test_author_sessions_filter_expiry_host_path_and_reload(tmp_path):
    state = {
        "cookies": [
            {"name": "auth", "value": "ok", "domain": ".bilibili.com", "path": "/", "expires": time.time() + 60},
            {"name": "host_only", "value": "www", "domain": "www.bilibili.com", "path": "/"},
            {"name": "restricted", "value": "path", "domain": ".bilibili.com", "path": "/private"},
            {"name": "expired", "value": "no", "domain": ".bilibili.com", "expires": 1},
            {"name": "foreign", "value": "no", "domain": ".douyin.com"},
        ],
        "origins": [{"origin": "https://www.bilibili.com", "localStorage": []}, {"origin": "https://evilbilibili.com", "localStorage": []}],
    }
    path = tmp_path / "bilibili.json"
    path.write_text(json.dumps(state))
    crawler = AuthorCrawler(client=SimpleNamespace(), strict_anonymous_mode=True, author_session_store=PlatformSessionStore(tmp_path))
    assert crawler.cookie_for_url("https://api.bilibili.com/x/view") == "auth=ok"
    assert "host_only=www" in crawler.cookie_for_url("https://www.bilibili.com/")
    assert "restricted=path" in crawler.cookie_for_url("https://api.bilibili.com/private/one")
    assert "restricted" not in crawler.cookie_for_url("https://api.bilibili.com/private-other")
    assert crawler.cookie_for_url("https://bilibili.com.evil.com/") == ""
    assert len(crawler.browser_state("bilibili")["origins"]) == 1
    state["cookies"][0]["value"] = "renewed"
    path.write_text(json.dumps(state))
    assert crawler.cookie_for_url("https://api.bilibili.com/") == "auth=renewed"


def test_author_cookie_is_isolated_from_general_crawler_and_other_platforms():
    settings = Settings(_env_file=None, proxy_51_api_url="https://example.com/proxy", weibo_author_cookie="SUB=account", author_use_saved_sessions=True, author_guest_only=False)
    service = EngagementService.from_settings(settings)
    crawler = AuthorService(service)._crawler()
    assert service.crawler._platform_cookie("weibo") == ""
    assert crawler.cookie_for_url("https://weibo.com/ajax/statuses/mymblog") == "SUB=account"
    assert crawler.cookie_for_url("https://www.douyin.com/") == ""
    assert crawler._session_headers("https://weibo.com/", {"Cookie": "SUB=private-lease"}) == {"Cookie": "SUB=private-lease"}
    asyncio.run(service.aclose())


@pytest.mark.parametrize("page", [1, 4])
def test_weibo_login_card_is_failure_even_with_ok_one(page):
    crawler = Replies({"ok": 1, "data": {"cards": [{"card_type": 7, "desc": "登录注册后查看更多微博"}], "cardlistInfo": {"total": 0}}})
    with pytest.raises(AuthorSessionError, match="登录"):
        asyncio.run(weibo.fetch_page(crawler, AuthorRequest(author_id="123", media_name="weibo"), {"page": page}))


def test_weibo_authenticated_timeline_and_long_text_use_account_api():
    crawler = Replies(
        {"ok": 1, "data": {"list": [{"idstr": "456", "user": {"id": 123}, "isLongText": True, "text_raw": "摘要"}]}},
        {"ok": 1, "data": {"longTextContent": "<p>完整长正文</p>"}}, cookie="SUB=account",
    )
    page = asyncio.run(weibo.fetch_page(crawler, AuthorRequest(author_id="123", media_name="weibo"), {}))
    assert page.data[0].text == "完整长正文"
    assert [url.split("/ajax/")[-1] for url, _ in crawler.calls] == ["statuses/mymblog", "statuses/longtext"]
    assert decode_cursor(AuthorRequest(author_id="123", media_name="weibo"), page.next_cursor)["source"] == "desktop"


def test_bilibili_independent_submissions_then_articles_then_dynamics():
    crawler = Replies(
        {"code": 0, "data": {"list": {"vlist": [{"mid": 123, "bvid": "BV123abc", "title": "独立投稿"}]}, "page": {"count": 1}}},
        {"code": 0, "data": {"articles": [{"id": 456, "author": {"mid": 123}, "title": "独立专栏"}], "count": 1}},
        {"code": 0, "data": {"items": [], "has_more": False}},
    )
    crawler._author_bili_session = ({"Cookie": "buvid3=visitor"}, "key")
    request = AuthorRequest(author_id="123", media_name="bilibili")
    async def run():
        first = await bilibili.fetch_page(crawler, request, {}, include_details=False)
        assert first.data[0].work_id == "BV123abc" and first.data[0].detail_status == "pending"
        second = await bilibili.fetch_page(crawler, request, decode_cursor(request, first.next_cursor), include_details=False)
        assert second.data[0].work_id == "cv456"
        third = await bilibili.fetch_page(crawler, request, decode_cursor(request, second.next_cursor), include_details=False)
        assert third.next_cursor is None
    asyncio.run(run())
    assert "/wbi/arc/search" in crawler.calls[0][0]
    assert "w_rid" in crawler.calls[0][1]["params"]


def test_bilibili_followup_page_keeps_visitor_context_and_signs_web_query():
    from hashlib import md5
    from urllib.parse import urlencode

    # Real author validation: five video pages (30/30/30/30/23), then articles.
    crawler = Replies({"code": 0, "data": {
        "list": {"vlist": [
            {"mid": 123, "bvid": f"BV1test{i}", "title": "视频"}
            for i in range(23)
        ]}, "page": {"count": 143},
    }})
    session = {"Cookie": "buvid3=current-egress", "Origin": "https://space.bilibili.com"}
    crawler._author_bili_session = (session, "key")
    request = AuthorRequest(author_id="123", media_name="bilibili")
    page = asyncio.run(bilibili.fetch_page(
        crawler, request, {"source": "video", "page": 5}, include_details=False,
    ))
    assert len(page.data) == 23
    assert decode_cursor(request, page.next_cursor)["source"] == "article"
    _, call = crawler.calls[0]
    assert call["headers"] == session
    params = dict(call["params"])
    signature = params.pop("w_rid")
    assert params["mid"] == "123" and params["pn"] == "5"
    assert params["platform"] == "web" and params["web_location"] == "1550101"
    assert params["dm_img_list"] == "[]"
    assert params["dm_img_str"] == params["dm_cover_img_str"] == "bm8gd2ViZ2"
    assert json.loads(params["dm_img_inter"]) == {"ds": [], "wh": [], "of": []}
    # Device parameters must be covered by the signature, not added afterward.
    assert signature == md5((urlencode(sorted(params.items())) + "key").encode()).hexdigest()


def test_bilibili_missing_article_list_requires_independent_zero_count():
    request = AuthorRequest(author_id="123", media_name="bilibili")
    for count in [0, 3]:
        crawler = Replies({"code": 0, "data": {"pn": 1, "ps": 30}}, {"code": 0, "data": {"article": count}})
        crawler._author_bili_session = ({}, "key")
        if count == 0:
            page = asyncio.run(bilibili.fetch_page(crawler, request, {"source": "article"}, include_details=False))
            assert page.data == [] and page.next_cursor
        else:
            with pytest.raises(PlatformCrawlerError, match="缺少作品列表"):
                asyncio.run(bilibili.fetch_page(crawler, request, {"source": "article"}, include_details=False))


@pytest.mark.parametrize("account", [False, True])
def test_bilibili_refreshes_guest_device_per_lease_but_keeps_account(account):
    cookie = "buvid3=previous-egress; buvid_fp=old"
    if account:
        cookie += "; SESSDATA=account"
    crawler = Replies(
        {"code": 0, "data": {"b_3": "fresh-device", "b_4": "fresh-device-4"}},
        {"code": -101, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png", "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"}}},
        cookie=cookie,
    )
    headers, _ = asyncio.run(bilibili.prepare_session(crawler, AuthorRequest(author_id="123", media_name="bilibili")))
    assert headers["Origin"] == "https://space.bilibili.com"
    if account:
        assert "SESSDATA=account" in headers["Cookie"]
        assert "buvid3=previous-egress" in headers["Cookie"]
    else:
        assert "buvid3=fresh-device" in headers["Cookie"]
        assert "previous-egress" not in headers["Cookie"] and "buvid_fp" not in headers["Cookie"]


def test_kuaishou_graphql_fallback_uses_verified_id_and_preserves_public_id():
    crawler = Replies({"result": 50}, {"data": {"visionProfilePhotoList": {"result": 1, "pcursor": "no_more", "feeds": [{"author": {"id": "3xprofile"}, "photo": {"id": "3xvideo", "caption": "文案", "photoUrl": "https://example.com/video"}}]}}})
    crawler._author_identity_cache = {"123": "3xprofile"}
    page = asyncio.run(kuaishou.fetch_page(crawler, AuthorRequest(author_id="123", media_name="kuaishou"), {}))
    assert page.data[0].author_uid == "123" and page.data[0].resolved_author_uid == "3xprofile"
    assert json.loads(crawler.calls[-1][1]["data"])["variables"]["userId"] == "3xprofile"


def test_xhs_authenticated_feed_preserves_full_text_media_and_owner(monkeypatch):
    uid, wid = "a" * 24, "b" * 24
    monkeypatch.setattr(xiaohongshu, "signed_headers", lambda *args, **kwargs: {"X-S": "test"})
    note = {"note_id": wid, "user": {"user_id": uid}, "title": "标题", "desc": "完整正文", "image_list": [{"url_default": "https://example.com/image"}], "video": {"media": {"stream": {"h264": [{"master_url": "https://example.com/video"}]}}}}
    request = AuthorRequest(author_id=uid, media_name="xiaohongshu")
    work = AuthorWork(work_id=wid, author_uid=uid, media_name="xiaohongshu", content_type="video", url=f"https://www.xiaohongshu.com/explore/{wid}", detail_context={"xsec_token": "token"})
    crawler = Replies({"success": True, "data": {"items": [{"id": wid, "note_card": note}]}}, cookie="a1=device; web_session=account")
    detail = asyncio.run(xiaohongshu.fetch_detail(crawler, request, work))
    assert detail.text == "完整正文" and len(detail.media_urls) == 2
    note["user"]["user_id"] = "c" * 24
    crawler = Replies({"success": True, "data": {"items": [{"id": wid, "note_card": note}]}}, cookie="a1=device")
    with pytest.raises(PlatformCrawlerError, match="作者不匹配"):
        asyncio.run(xiaohongshu.fetch_detail(crawler, request, work))


def test_toutiao_login_page_is_not_article_content():
    request = AuthorRequest(author_id="123", media_name="toutiao")
    work = AuthorWork(work_id="456", author_uid="123", media_name="toutiao", content_type="article", url="https://www.toutiao.com/article/456/")
    with pytest.raises(AuthorSessionError, match="登录页"):
        asyncio.run(toutiao.fetch_detail(Replies("<title>今日头条登录</title><article>登录协议</article>"), request, work))
