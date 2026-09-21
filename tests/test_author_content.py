from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.api.app import create_app
from app.api.routers.authors import get_author_service
from app.crawlers.authors import bilibili, douyin, sina_blog, weibo
from app.crawlers.authors.common import decode_cursor, encode_cursor
from app.crawlers.http_client import PlatformCrawlerError
from app.models.author import AuthorPage, AuthorRequest


class Crawler:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def _get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def _get_response(self, url, **kwargs):
        return SimpleNamespace(text=await self._get_json(url, **kwargs))

    async def _ensure_weibo_visitor_session(self, *args, **kwargs):
        pass

    def _weibo_visitor_headers(self):
        return {}

    def _platform_cookie(self, platform):
        return ""


def test_author_input_and_bound_cursor():
    request = AuthorRequest(uid="123", platform="微博")
    assert request.media_name == "weibo"
    cursor = encode_cursor(request, {"page": 2})
    assert decode_cursor(request, cursor)["page"] == 2
    with pytest.raises(ValueError):
        decode_cursor(AuthorRequest(uid="456", media_name="weibo"), cursor)
    for uid, platform in [
        ("https://example.com", "weibo"),
        ("a/b", "douyin"),
        ("123", "wechat"),
    ]:
        with pytest.raises(ValidationError):
            AuthorRequest(uid=uid, media_name=platform)


def test_sina_article_body_owner_filter_and_pagination():
    listing = """<div class="articleCell"><a href="/s/blog_0000007b01001abc.html">文章</a>2026-09-01</div>
    <div class="articleCell"><a href="https://evil.invalid/s/blog_0000007b01001abc.html">重复</a>2026-09-01</div>
    <div class="articleCell"><a href="/s/blog_0000007c01001abc.html">别的作者</a>2026-09-01</div>
    <a href="/s/articlelist_123_0_2.html">下一页</a>"""
    detail = '<div class="articalTitle"><h2>完整标题</h2></div><div class="articalContent">正文一<p>正文二</p></div>'
    crawler = Crawler(listing, detail)
    request = AuthorRequest(uid="123", media_name="sina_blog")
    page = asyncio.run(sina_blog.fetch_page(crawler, request, {}))
    assert len(page.data) == 1
    assert page.data[0].title == "完整标题"
    assert page.data[0].text == "正文一\n正文二"
    assert page.data[0].author_uid == "123"
    assert page.data[0].content_complete
    assert decode_cursor(request, page.next_cursor)["page"] == 2
    assert all(url.startswith("https://blog.sina.com.cn/") for url, _ in crawler.calls)


def test_sina_failed_body_is_partial_not_silent_empty_success():
    crawler = Crawler(
        '<div class="articleCell"><a href="/s/blog_0000007b01001abc.html">文</a>2026-09-01</div>',
        "<html>验证页面</html>",
    )
    page = asyncio.run(
        sina_blog.fetch_page(
            crawler, AuthorRequest(uid="123", media_name="sina_blog"), {}
        )
    )
    assert not page.data[0].content_complete
    assert page.data[0].error


def test_weibo_uid_filter_long_text_and_partial_coverage():
    post = {
        "id": "1000",
        "user": {"id": 123, "screen_name": "作者"},
        "text": "截断",
        "isLongText": True,
        "created_at": "Fri Sep 18 12:00:00 +0800 2026",
    }
    other = {**post, "id": "1001", "user": {"id": 456}}
    crawler = Crawler(
        {
            "ok": 1,
            "data": {
                "cards": [
                    {"card_type": 9, "mblog": post},
                    {"card_type": 9, "mblog": other},
                ]
            },
        },
        {"ok": 1, "data": {"longTextContent": "<p>全部正文</p>"}},
    )
    page = asyncio.run(
        weibo.fetch_page(crawler, AuthorRequest(uid="123", media_name="weibo"), {})
    )
    assert len(page.data) == 1 and page.data[0].text == "全部正文"
    assert page.coverage == "partial"
    assert page.next_cursor


def test_douyin_public_content_and_owner_validation(monkeypatch):
    async def session(*args):
        return "anonymous"

    monkeypatch.setattr(douyin, "prepare_anonymous_session", session)
    uid = "MS4wLjABAAAA0123456789abcdef"
    request = AuthorRequest(uid=uid, media_name="douyin")
    post = {
        "aweme_id": "123",
        "author": {"sec_uid": uid, "nickname": "作者"},
        "desc": "视频简介",
        "create_time": 1700000000,
        "video": {
            "play_addr": {"url_list": ["https://example.com/video.mp4"]},
            "duration": 1000,
        },
    }
    crawler = Crawler(
        {"status_code": 0, "aweme_list": [post], "has_more": 1, "max_cursor": 1234}
    )
    page = asyncio.run(douyin.fetch_page(crawler, request, {}))
    assert page.data[0].description == "视频简介"
    assert page.data[0].media_urls == ["https://example.com/video.mp4"]
    assert decode_cursor(request, page.next_cursor)["max_cursor"] == "1234"
    assert page.coverage == "partial"
    post["author"]["sec_uid"] = "other"
    with pytest.raises(PlatformCrawlerError, match="作者"):
        asyncio.run(
            douyin.fetch_page(
                Crawler({"status_code": 0, "aweme_list": [post], "has_more": 0}),
                request,
                {},
            )
        )


def test_douyin_rejects_non_advancing_cursor(monkeypatch):
    async def session(*args):
        return "anonymous"

    monkeypatch.setattr(douyin, "prepare_anonymous_session", session)
    request = AuthorRequest(uid="MS4wLjABAAAA0123456789abcdef", media_name="douyin")
    with pytest.raises(PlatformCrawlerError, match="游标"):
        asyncio.run(
            douyin.fetch_page(
                Crawler(
                    {
                        "status_code": 0,
                        "aweme_list": [],
                        "has_more": 1,
                        "max_cursor": 123,
                    }
                ),
                request,
                {"max_cursor": "123"},
            )
        )


def test_bilibili_video_content_and_pagination():
    request = AuthorRequest(uid="123", media_name="bilibili")
    item = {
        "id_str": "1234",
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"mid": 123, "name": "作者", "pub_ts": 1700000000},
            "module_dynamic": {
                "major": {"archive": {"bvid": "BV123456", "title": "视频标题"}}
            },
        },
    }
    crawler = Crawler(
        {"code": 0, "data": {"items": [item], "has_more": True, "offset": "5678"}},
        {
            "code": 0,
            "data": {
                "bvid": "BV123456",
                "owner": {"mid": 123},
                "desc": "完整简介",
                "duration": 10,
            },
        },
    )
    page = asyncio.run(bilibili.fetch_page(crawler, request, {"source": "dynamic"}))
    assert page.data[0].description == "完整简介"
    assert page.data[0].duration_ms == 10000
    assert page.next_cursor


def test_author_page_api_validation_and_upstream_failure():
    class Service:
        async def fetch_page(self, request, cursor):
            if request.uid == "456":
                raise PlatformCrawlerError("平台拒绝")
            return AuthorPage(
                uid=request.uid, media_name=request.media_name, reason="公开列表"
            )

    async def run():
        app = create_app()
        app.dependency_overrides[get_author_service] = lambda: Service()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/authors/works", params={"uid": "123", "media_name": "微博"}
            )
            assert response.status_code == 200 and response.json()["uid"] == "123"
            response = await client.get(
                "/api/v1/authors/works", params={"uid": "456", "media_name": "微博"}
            )
            assert response.status_code == 502
            response = await client.get(
                "/api/v1/authors/works", params={"uid": "123", "media_name": "公众号"}
            )
            assert response.status_code == 422
            response = await client.post(
                "/api/v1/authors/jobs", json={"uid": "123", "media_name": "公众号"}
            )
            # Without a manager dependency, the service is explicitly unavailable.
            assert response.status_code in {422, 503}

    asyncio.run(run())


def test_author_service_shares_capacity_and_rotates_semantic_failures(monkeypatch):
    from contextlib import asynccontextmanager
    from app.services.author_service import AuthorService, HANDLERS
    from app.services.engagement_service import EngagementService

    class Client:
        invalidations = 0

        @asynccontextmanager
        async def isolated_session_scope(self):
            yield

        @asynccontextmanager
        async def lease_scope(self):
            yield

        def invalidate_active_lease(self, reason):
            self.invalidations += 1

    async def run():
        client = Client()
        service = EngagementService(
            SimpleNamespace(client=client), collection_max_concurrency=1
        )
        author = AuthorService(service, page_timeout_seconds=0.03)
        attempts = 0

        async def handler(crawler, request, state):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PlatformCrawlerError("HTTP 200 without list")
            return AuthorPage(uid=request.uid, media_name=request.media_name)

        monkeypatch.setitem(HANDLERS, "sina_blog", handler)
        request = AuthorRequest(uid="123", media_name="sina_blog")
        async with service._collection_semaphore:
            with pytest.raises(TimeoutError):
                await author.fetch_page(request)
        assert attempts == 0
        assert (await author.fetch_page(request)).uid == "123"
        assert attempts == 2 and client.invalidations == 1

    asyncio.run(run())


def test_sina_tags_block_is_not_article_body():
    listing = '<div class="articleCell"><a href="/s/blog_0000007b01001abc.html">文</a>2026-09-01</div>'
    detail = '<div id="sina_keyword_ad_area">标签：股票</div><div id="sina_keyword_ad_area2" class="articalContent">真正正文</div>'
    request = AuthorRequest(uid="123", media_name="sina_blog")
    page = asyncio.run(sina_blog.fetch_page(Crawler(listing, detail), request, {}))
    assert page.data[0].text == "真正正文"
    tags_only = '<div id="sina_keyword_ad_area">标签：股票</div>'
    page = asyncio.run(sina_blog.fetch_page(Crawler(listing, tags_only), request, {}))
    assert not page.data[0].content_complete
    assert page.data[0].text == ""


@pytest.mark.parametrize(
    "platform,uid,normalized",
    [
        ("抖音", "MS4wLjABAAAA0123456789abcdef", "douyin"),
        ("快手", "3xabc123456", "kuaishou"),
        ("微博", "123", "weibo"),
        ("b站", "123", "bilibili"),
        ("小红书", "6667a191000000000700539e", "xiaohongshu"),
        ("今日头条", "MS4wLjABAAAAabcdefghijk", "toutiao"),
    ],
)
def test_six_author_platforms(platform, uid, normalized):
    assert AuthorRequest(uid=uid, platform=platform).media_name == normalized


def test_list_discovery_does_not_fetch_article_bodies():
    crawler = Crawler(
        '<div class="articleCell"><a href="/s/blog_0000007b01001abc.html">文</a></div>'
    )
    page = asyncio.run(
        sina_blog.fetch_page(
            crawler,
            AuthorRequest(uid="123", media_name="sina_blog"),
            {},
            include_details=False,
        )
    )
    assert len(crawler.calls) == 1
    assert page.data[0].detail_status == "pending" and not page.data[0].content_complete


def test_kuaishou_rejects_login_and_checks_owner():
    from app.crawlers.authors import kuaishou

    class PostCrawler(Crawler):
        async def _post_json(self, url, **kwargs):
            return await self._get_json(url, **kwargs)

    request = AuthorRequest(uid="3xauthor", media_name="kuaishou")
    with pytest.raises(PlatformCrawlerError, match="109"):
        asyncio.run(kuaishou.fetch_page(PostCrawler({"result": 109}, {"data": {}}), request, {}))
    payload = {
        "result": 1,
        "pcursor": "next",
        "feeds": [
            {
                "author": {"id": "3xauthor"},
                "photo": {
                    "id": "3xwork",
                    "caption": "完整文案",
                    "photoUrl": "https://example.com/video.mp4",
                },
            }
        ],
    }
    page = asyncio.run(kuaishou.fetch_page(PostCrawler(payload), request, {}))
    assert page.data[0].content_complete and page.next_cursor
    payload["feeds"][0]["author"]["id"] = "other"
    with pytest.raises(PlatformCrawlerError, match="作者"):
        asyncio.run(kuaishou.fetch_page(PostCrawler(payload), request, {}))


def test_xhs_token_survives_list_to_detail_and_wrong_author_rejected():
    import json
    from app.crawlers.authors import xiaohongshu

    uid, wid = "6667a191000000000700539e", "6a5ee843000000001302cf06"
    request = AuthorRequest(uid=uid, media_name="xiaohongshu")

    class Browser(Crawler):
        async def captured(self, *args, **kwargs):
            return {
                "success": True,
                "data": {
                    "has_more": False,
                    "notes": [
                        {
                            "note_id": wid,
                            "xsec_token": "current/token=",
                            "user": {"user_id": uid},
                            "display_title": "题",
                        }
                    ],
                },
            }

    page = asyncio.run(xiaohongshu.fetch_page(Browser(), request, {}))
    item = page.data[0]
    assert item.detail_context["xsec_token"] == "current/token="
    assert "xsec_token=current%2Ftoken%3D" in item.url
    note = {
        "noteId": wid,
        "user": {"userId": uid},
        "title": "完整标题",
        "desc": "undefined 是正文，不应被替换",
        "time": 1700000000000,
    }
    html = (
        "<script>window.__INITIAL_STATE__="
        + json.dumps(
            {"note": {"noteDetailMap": {wid: {"note": note}}}}, ensure_ascii=False
        )
        + "</script>"
    )
    result = asyncio.run(xiaohongshu.fetch_detail(Crawler(html), request, item))
    assert result.text == "undefined 是正文，不应被替换"
    with pytest.raises(PlatformCrawlerError, match="作者"):
        asyncio.run(
            xiaohongshu.fetch_detail(Crawler(html.replace(uid, "other")), request, item)
        )


def test_author_discovery_preserves_session_and_rotates_before_expiry(monkeypatch):
    from contextlib import asynccontextmanager
    from contextvars import ContextVar
    from app.services.author_service import AuthorService, HANDLERS
    from app.services.engagement_service import EngagementService

    class Client:
        def __init__(self):
            self.current = ContextVar("session", default=None)
            self.sequence = 0
            self.remaining = 170

        @asynccontextmanager
        async def isolated_session_scope(self):
            self.sequence += 1
            token = self.current.set(self.sequence)
            try:
                yield
            finally:
                self.current.reset(token)

        @asynccontextmanager
        async def lease_scope(self):
            yield

        def invalidate_active_lease(self, reason):
            pass

        def active_lease_remaining_seconds(self):
            return self.remaining if self.current.get() else None

    async def run():
        client = Client()
        service = AuthorService(EngagementService(SimpleNamespace(client=client)))
        sessions = []

        async def handler(crawler, request, state):
            sessions.append(client.current.get())
            return AuthorPage(uid=request.uid, media_name=request.media_name)

        monkeypatch.setitem(HANDLERS, "sina_blog", handler)
        request = AuthorRequest(uid="123", media_name="sina_blog")
        async with service.discovery_session(request) as source:
            await source.fetch_page(request)
            await source.fetch_page(request)
            client.remaining = 15
            await source.fetch_page(request)
        assert sessions == [1, 1, 2]
        assert client.current.get() is None

    asyncio.run(run())


def test_toutiao_owner_cursor_and_full_body_validation():
    from app.crawlers.authors import toutiao
    from app.models.author import AuthorWork

    request = AuthorRequest(uid="MS4wLjABAAAAcreator", media_name="toutiao")
    payload = {
        "message": "success",
        "has_more": True,
        "next": {"max_behot_time": 1700000000000},
        "data": [
            {"item_id": "1234", "title": "标题", "user_info": {"user_id": request.uid}}
        ],
    }
    page = toutiao.parse_page(payload, request, "0")
    assert page.data[0].detail_status == "pending"
    assert decode_cursor(request, page.next_cursor)["max_behot_time"] == "1700000000000"
    payload["data"][0]["user_info"]["user_id"] = "other"
    with pytest.raises(PlatformCrawlerError, match="作者"):
        toutiao.parse_page(payload, request, "0")
    item = AuthorWork(
        work_id="1234",
        author_uid=request.uid,
        media_name="toutiao",
        content_type="article",
        url="https://www.toutiao.com/article/1234/",
    )
    html = '<script type="application/ld+json">{"@type":"NewsArticle","mainEntityOfPage":{"@id":"https://www.toutiao.com/article/1234/"}}</script><article>完整<p>正文</p></article>'
    result = asyncio.run(toutiao.fetch_detail(Crawler(html), request, item))
    assert result.text == "完整\n正文"
    with pytest.raises(PlatformCrawlerError, match="ID"):
        asyncio.run(
            toutiao.fetch_detail(
                Crawler(html.replace("/1234/", "/9999/")), request, item
            )
        )


def test_discovery_and_detail_tasks_use_independent_proxy_contexts(monkeypatch):
    from contextlib import asynccontextmanager
    from contextvars import ContextVar
    from app.models.author import AuthorWork
    from app.services.author_service import AuthorService, HANDLERS, ADAPTERS
    from app.services.engagement_service import EngagementService

    class Client:
        def __init__(self):
            self.lease = ContextVar("lease", default=None)
            self.cookie = ContextVar("cookie", default=None)
            self.number = 0

        @asynccontextmanager
        async def isolated_session_scope(self):
            value = object()
            token = self.cookie.set(value)
            try:
                yield
            finally:
                self.cookie.reset(token)

        @asynccontextmanager
        async def lease_scope(self):
            self.number += 1
            token = self.lease.set(self.number)
            try:
                yield
            finally:
                self.lease.reset(token)

    async def run():
        client = Client()
        service = AuthorService(EngagementService(SimpleNamespace(client=client)))
        request = AuthorRequest(uid="123", media_name="sina_blog")
        entered, release = asyncio.Event(), asyncio.Event()
        observed = []

        async def listing(crawler, request, state):
            observed.append((client.lease.get(), client.cookie.get()))
            entered.set()
            await release.wait()
            return AuthorPage(uid=request.uid, media_name=request.media_name)

        async def detail(crawler, request, work):
            observed.append((client.lease.get(), client.cookie.get()))
            release.set()
            return work

        monkeypatch.setitem(HANDLERS, "sina_blog", listing)
        monkeypatch.setattr(ADAPTERS["sina_blog"], "fetch_detail", detail)

        async def discover():
            async with service.discovery_session(request) as source:
                await source.fetch_page(request)

        task = asyncio.create_task(discover())
        await entered.wait()
        work = AuthorWork(
            work_id="1",
            author_uid="123",
            media_name="sina_blog",
            content_type="article",
            url="https://blog.sina.com.cn/s/1.html",
        )
        await service.fetch_detail(request, work)
        await task
        assert observed[0][0] != observed[1][0]
        assert observed[0][1] is not observed[1][1]
        assert client.lease.get() is None and client.cookie.get() is None

    asyncio.run(run())


def test_author_reuses_only_explicit_xhs_cookie_configuration():
    from app.services.author_service import AuthorService, DiscoverySession
    from app.services.engagement_service import EngagementService

    parent = SimpleNamespace(
        client=object(),
        xiaohongshu_cookie_enabled=True,
        platform_cookies={
            "xiaohongshu": "a1=test; web_session=configured",
            "weibo": "do-not-copy",
        },
    )
    service = AuthorService(EngagementService(parent, author_guest_only=False))
    crawler = service._crawler()
    assert crawler._platform_cookie("xiaohongshu") == "a1=test; web_session=configured"
    assert crawler._platform_cookie("weibo") == ""
    assert not DiscoverySession(
        service, AuthorRequest(uid="6667a191000000000700539e", media_name="xiaohongshu")
    ).browser_mode
    parent.xiaohongshu_cookie_enabled = False
    crawler = service._crawler()
    assert crawler._platform_cookie("xiaohongshu") == ""


def test_toutiao_embedded_video_remains_article():
    from app.crawlers.authors import toutiao

    request = AuthorRequest(uid="123", media_name="toutiao")
    page = toutiao.parse_page(
        {
            "message": "success",
            "has_more": False,
            "data": [
                {
                    "item_id": "456",
                    "has_video": False,
                    "video_id": "embedded-video",
                    "user_info": {"user_id": "123"},
                }
            ],
        },
        request,
        "0",
    )
    assert page.data[0].content_type == "article"
    assert page.data[0].url == "https://www.toutiao.com/article/456/"


@pytest.mark.parametrize(
    "account,platform,normalized",
    [
        ("ChenNan12.24", "抖音", "douyin"),
        ("46219502311", "抖音", "douyin"),
        ("27247934734", "小红书", "xiaohongshu"),
        ("Mango_Foodie", "小红书", "xiaohongshu"),
        ("1761218769", "快手", "kuaishou"),
        ("7946463148", "新浪微博", "weibo"),
        ("100515238009", "今日头条微头条", "toutiao"),
        ("Cix4ZolmTrBM_GJmTLUMBR1dqACv3JH2i8LJqCGIbEdce=", "今日头条", "toutiao"),
        ("3745058316618628", "哔哩哔哩", "bilibili"),
    ],
)
def test_real_author_id_shapes(account, platform, normalized):
    value = AuthorRequest(author_id=account, media_name=platform)
    assert value.uid == account and value.media_name == normalized


def test_author_id_api_contract_and_conflicting_legacy_uid():
    class Service:
        async def fetch_page(self, request, cursor):
            assert request.media_name == "weibo"
            return AuthorPage(uid=request.uid, media_name=request.media_name)

    async def run():
        app = create_app()
        app.dependency_overrides[get_author_service] = lambda: Service()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/authors/works",
                params={"author_id": "7946463148", "media_name": "新浪微博"},
            )
            assert response.status_code == 200
            assert (
                response.json()["author_id"] == response.json()["uid"] == "7946463148"
            )
            response = await client.get(
                "/api/v1/authors/works",
                params={"author_id": "123", "uid": "456", "media_name": "微博"},
            )
            assert response.status_code == 422
            assert (
                await client.get("/api/v1/authors/works", params={"media_name": "微博"})
            ).status_code == 422

    asyncio.run(run())


def test_public_account_resolution_requires_exact_unique_match():
    from app.crawlers.authors.identity import select_douyin_user, select_xhs_user

    uid = "MS4wLjABAAAA0123456789abcdef"
    user = {"sec_uid": uid, "unique_id": "ChenNan12.24", "nickname": "someone"}
    payload = {"status_code": 0, "user_list": [{"user_info": user}]}
    assert select_douyin_user(payload, "ChenNan12.24")["sec_uid"] == uid
    with pytest.raises(PlatformCrawlerError, match="完全匹配"):
        select_douyin_user(payload, "someone")
    payload["user_list"].append({"user_info": {**user, "sec_uid": uid + "other"}})
    with pytest.raises(PlatformCrawlerError, match="唯一"):
        select_douyin_user(payload, "ChenNan12.24")
    xhs = {
        "success": True,
        "data": {
            "users": [
                {
                    "user_id": "6667a191000000000700539e",
                    "red_id": "27247934734",
                    "nickname": "hello",
                }
            ]
        },
    }
    assert select_xhs_user(xhs, "27247934734") == "6667a191000000000700539e"
    with pytest.raises(PlatformCrawlerError, match="完全匹配"):
        select_xhs_user(xhs, "hello")


def test_numeric_douyin_handle_falls_back_from_internal_uid_lookup(monkeypatch):
    from app.crawlers.authors.identity import resolve_douyin
    import app.crawlers.authors.identity as identity

    monkeypatch.setattr(identity, "signed_params", lambda value: value)
    uid = "MS4wLjABAAAA0123456789abcdef"
    c = Crawler(
        {"status_code": 0, "user": {"uid": "999", "sec_uid": uid + "wrong"}},
        {
            "status_code": 0,
            "user_list": [
                {
                    "user_info": {
                        "uid": "777",
                        "unique_id": "46219502311",
                        "sec_uid": uid,
                    }
                }
            ],
        },
    )
    assert asyncio.run(resolve_douyin(c, "46219502311", {}, {})) == uid
    assert c.calls[1][1]["params"]["keyword"] == "46219502311"


def test_toutiao_short_video_metadata_uses_numeric_owner_and_no_detail_request():
    from app.crawlers.authors import toutiao

    request = AuthorRequest(author_id="3402362465301307", media_name="今日头条")
    entry = {
        "item_id": "7686944261303959598",
        "title": "公开视频文案",
        "itemCell": {"userInfo": {"userID": 3402362465301307, "name": "星车探"}},
        "video": {
            "play_addr": {"url_list": ["https://example.com/movie.mp4"]},
            "duration": 94,
        },
    }
    payload = {"message": "success", "has_more": False, "data": [entry]}
    page = toutiao.parse_page(payload, request, "0", profile_token="redirected-token=")
    work = page.data[0]
    assert work.content_type == "video" and work.detail_status == "complete"
    assert work.text == entry["title"] and work.duration_ms == 94000
    assert work.media_urls == ["https://example.com/movie.mp4"]
    assert work.model_dump()["author_id"] == request.uid
    entry["itemCell"]["userInfo"]["userID"] = 999
    with pytest.raises(PlatformCrawlerError, match="作者"):
        toutiao.parse_page(payload, request, "0", profile_token=request.uid)


def test_toutiao_detail_accepts_verified_list_owner_alias_and_canonical_route():
    from app.crawlers.authors import toutiao

    request = AuthorRequest(author_id="123", media_name="今日头条")
    page = toutiao.parse_page(
        {
            "message": "success",
            "has_more": False,
            "data": [
                {
                    "item_id": "456",
                    "user_info": {"user_id": "MS4wLjOtherIdentity"},
                    "itemCell": {"userInfo": {"userID": 123}},
                }
            ],
        },
        request,
        "0",
        profile_token="redirected=",
    )
    body = '<link rel="canonical" href="https://www.toutiao.com/group/456/"><script type="application/ld+json">{"@type":"NewsArticle","mainEntityOfPage":{"@id":"https://www.toutiao.com/article/456/"},"author":{"url":"https://www.toutiao.com/c/user/token/MS4wLjOtherIdentity/"}}</script><article>正文</article>'
    assert (
        asyncio.run(toutiao.fetch_detail(Crawler(body), request, page.data[0])).text
        == "正文"
    )
    with pytest.raises(PlatformCrawlerError, match="作者"):
        asyncio.run(
            toutiao.fetch_detail(
                Crawler(body.replace("MS4wLjOtherIdentity", "different")),
                request,
                page.data[0],
            )
        )


def test_kuaishou_mobile_detail_checks_original_numeric_id_and_work():
    import json
    from app.crawlers.authors.kuaishou import parse_mobile_detail
    from app.models.author import AuthorWork

    request = AuthorRequest(author_id="1761218769", media_name="快手")
    work = AuthorWork(
        work_id="3xh8q8ngakvtstg",
        author_uid=request.uid,
        media_name="kuaishou",
        content_type="video",
        url="https://www.kuaishou.com/short-video/3xh8q8ngakvtstg",
    )
    photo = {
        "userId": 1761218769,
        "userName": "一汽-大众",
        "caption": "完整文案",
        "share_info": "userId=3xscewmvnazsb7k&photoId=3xh8q8ngakvtstg",
        "mainMvUrls": [{"url": "https://example.com/v.mp4"}],
        "duration": 103503,
    }
    body = (
        "<script>window.INIT_STATE="
        + json.dumps({"opaque": {"photo": photo}})
        + "</script>"
    )
    result = parse_mobile_detail(body, request, work)
    assert result.text == "完整文案" and result.resolved_author_uid == "3xscewmvnazsb7k"
    assert result.media_urls == ["https://example.com/v.mp4"]
    with pytest.raises(PlatformCrawlerError, match="作者"):
        parse_mobile_detail(body.replace("1761218769", "999"), request, work)
    with pytest.raises(PlatformCrawlerError, match="作品 ID"):
        parse_mobile_detail(body.replace("3xh8q8ngakvtstg", "other"), request, work)


def test_weibo_first_page_invisible_is_not_reported_as_empty_success():
    with pytest.raises(PlatformCrawlerError, match="首屏访客不可见"):
        asyncio.run(
            weibo.fetch_page(
                Crawler({"ok": 0, "msg": "这里还没有内容"}),
                AuthorRequest(author_id="123", media_name="微博"),
                {},
            )
        )
