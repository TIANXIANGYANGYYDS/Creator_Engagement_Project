from __future__ import annotations

import asyncio
import hashlib
import json
import random
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.crawlers.authors import douyin, douyin_session
from app.crawlers.authors.browser import BrowserTransport
from app.crawlers.authors.common import AuthorCursorError
from app.crawlers.http_client import PlatformBlockedError
from app.crawlers.platforms.douyin_abogus_v2 import ABogus, DEFAULT_BROWSER_INFO
from app.models.author import AuthorRequest


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
PROFILE = {"user_agent": UA, "browser_info": DEFAULT_BROWSER_INFO, "params": {"aid": "6383"}}
SEC_UID = "MS4wLjABAAAA0123456789abcdef"


def test_abogus_matches_pinned_upstream_vector():
    # Generated independently with the pinned upstream and its pure-Python SM3.
    # This guards our OpenSSL adaptation and Unicode/percent-encoded inputs.
    value = ABogus(UA, rng=random.Random(7)).get_value(
        "aid=6383&keyword=%E4%BD%9C%E8%80%85%20A%2BB&max_cursor=0", now_ms=1789747200123
    )
    assert value == (
        "xvUbhttLmNWRPVKb8cr1H7MlKh6ANPSynUTxRrBTyPF7Lw0T4bNRzxbWnozNskRD"
        "3Ypwkq17unz/YEdcp4UwpeHpLmpDuEvR7t5cnW0o2qqmGzvQLNbhezvFwwMnURGqa"
        "A5Ui1U62UrH6nVAhqQm/pA9HKLCQ5WBP1ORk2zcT9BhZ0LAgZcrPpbpNwit0f2j"
    )


def test_web_signature_covers_exact_wire_query_and_current_cookies():
    url = douyin_session.signed_url(
        "https://www.douyin.com/aweme/v1/web/aweme/post/",
        {"keyword": "作者 A+B", "a_bogus": "stale", "uifid": "other-proxy", "timestamp": "1"},
        {"UIFID_TEMP": "visitor", "s_v_web_id": "verify_current", "msToken": "real+/="},
        PROFILE, now_ms=1789747200123,
    )
    query, signature = urlsplit(url).query.rsplit("&x-secsdk-web-signature=", 1)
    params = parse_qs(query)
    assert params["keyword"] == ["作者 A+B"]
    assert "%20" in query and "%2B" in query
    assert params["uifid"] == ["visitor"]
    assert params["verifyFp"] == params["fp"] == ["verify_current"]
    assert params["msToken"] == ["real+/="]
    assert params["timestamp"] == ["1789747200"]
    assert params["a_bogus"] != ["stale"]
    expected = hashlib.md5(
        ("visitor_1789747200_A96D855A08C0A9707F8BEF0D9A527E4E_" + query).encode()
    ).hexdigest()
    assert signature == expected
    with pytest.raises(PlatformBlockedError, match="UIFID"):
        douyin_session.signed_url("https://www.douyin.com/", {}, {}, PROFILE)
    no_token = douyin_session.signed_url(
        "https://www.douyin.com/", {"msToken": "unrelated-generated-token"},
        {"UIFID_TEMP": "visitor"}, PROFILE,
    )
    assert "msToken" not in parse_qs(urlsplit(no_token).query)


def test_browser_falls_back_to_verified_sdk_on_empty_native_payload():
    class Page:
        context = None

        def __init__(self):
            self.context = self
            self.calls = []

        async def cookies(self, origin):
            return [{"name": "UIFID_TEMP", "value": "visitor"}]

        async def evaluate(self, script, args=None):
            if args is None:
                return PROFILE
            self.calls.append(args)
            return {"status": 200, "text": "" if args["native"] else json.dumps(
                {"status_code": 0, "aweme_list": [], "has_more": 0}
            )}

    async def run():
        transport = BrowserTransport(SimpleNamespace(client=None), asyncio.Semaphore(1))
        transport.page = Page()
        transport.douyin_sdk_ready = True
        result = await transport._get_json(
            "https://www.douyin.com/aweme/v1/web/aweme/post/", params={"max_cursor": "123"}
        )
        assert result["aweme_list"] == []
        assert transport.douyin_request_mode == "sdk"
        assert [c["native"] for c in transport.page.calls] == [True, False]
        assert all(parse_qs(urlsplit(c["url"]).query)["max_cursor"] == ["123"] for c in transport.page.calls)
        transport.douyin_sdk_ready = False
        transport.douyin_request_mode = "native"
        with pytest.raises(PlatformBlockedError, match="空包"):
            await transport._get_json("https://www.douyin.com/aweme/v1/web/aweme/post/")

    asyncio.run(run())


def test_verified_handle_mapping_accepts_sparse_post_author_and_rejects_forged_cursor(monkeypatch):
    async def cookie(*args):
        return "ttwid=test"

    monkeypatch.setattr(douyin, "prepare_anonymous_session", cookie)

    class Crawler:
        _author_identity_cache = {"public.handle": SEC_UID}

        async def _get_json(self, *args, **kwargs):
            return {"status_code": 0, "has_more": 0, "aweme_list": [
                {"aweme_id": "123", "desc": "full public description", "author": {"sec_uid": SEC_UID}}
            ]}

    request = AuthorRequest(author_id="public.handle", media_name="抖音")
    page = asyncio.run(douyin.fetch_page(Crawler(), request, {}))
    assert page.data[0].author_uid == "public.handle"
    assert page.data[0].description == "full public description"
    with pytest.raises(AuthorCursorError, match="身份"):
        asyncio.run(douyin.fetch_page(Crawler(), request, {"sec_uid": SEC_UID + "other"}))


def test_author_cookie_is_explicit_and_does_not_change_interaction_cookies():
    from app.core.config import Settings
    from app.services.author_service import AuthorService
    from app.services.engagement_service import EngagementService

    async def run():
        settings = Settings(_env_file=None, PROXY_51_API_URL="https://example.com/proxies",
                            DOUYIN_AUTHOR_COOKIE="sessionid=author-only", author_guest_only=False)
        for mode, expected in [("anonymous", ""), ("cookie", "sessionid=author-only")]:
            service = EngagementService.from_settings(settings.model_copy(
                update={"douyin_author_session_mode": mode}
            ))
            try:
                crawler = AuthorService(service)._crawler()
                assert crawler._author_douyin_configured_cookie == expected
                assert service.crawler._platform_cookie("douyin") == ""
                assert crawler._platform_cookie("weibo") == ""
                await crawler.aclose()
            finally:
                await service.aclose()
        missing = Settings(_env_file=None, DOUYIN_AUTHOR_SESSION_MODE="cookie", author_guest_only=False)
        with pytest.raises(ValueError, match="DOUYIN_AUTHOR_COOKIE"):
            EngagementService.from_settings(missing)

    asyncio.run(run())


def test_verified_identity_survives_browser_and_proxy_rotation(monkeypatch):
    from contextlib import asynccontextmanager
    from app.services import author_service
    from app.services.engagement_service import EngagementService
    from app.models.author import AuthorPage

    class Client:
        remaining = 170

        @asynccontextmanager
        async def isolated_session_scope(self):
            yield

        @asynccontextmanager
        async def lease_scope(self):
            self.remaining = 170
            yield

        def active_lease_remaining_seconds(self):
            return self.remaining

        def invalidate_active_lease(self, reason):
            pass

    opened, closed = [], []

    class Browser:
        def __init__(self, *args, **kwargs):
            pass

        async def open(self, url):
            opened.append(url)
            return self

        async def close(self):
            closed.append(True)

    async def handler(transport, request, state):
        if len(opened) == 1:
            transport._author_identity_cache[request.uid] = SEC_UID
        else:
            assert transport._author_identity_cache[request.uid] == SEC_UID
        return AuthorPage(uid=request.uid, media_name=request.media_name)

    monkeypatch.setattr(author_service, "BrowserTransport", Browser)
    monkeypatch.setitem(author_service.HANDLERS, "douyin", handler)

    async def run():
        client = Client()
        async def cooldown(platform):
            pass
        service = author_service.AuthorService(EngagementService(SimpleNamespace(
            client=client, _wait_for_circuit_cooldown=cooldown
        )))
        request = AuthorRequest(author_id="public.handle", media_name="douyin")
        async with service.discovery_session(request) as source:
            await source.fetch_page(request)
            client.remaining = 10
            await source.fetch_page(request)
        assert len(closed) == 2
        assert "/search/public.handle" in opened[0]
        assert opened[1] == f"https://www.douyin.com/user/{SEC_UID}"

    asyncio.run(run())


def test_persistent_mapping_is_revalidated_and_renamed_handle_is_invalidated():
    from app.crawlers.authors.identity import resolve_douyin, AuthorIdentityError

    class Repository:
        deleted = False

        async def get_identity(self, *args):
            return {"user": {"sec_uid": SEC_UID}}

        async def delete_identity(self, *args):
            self.deleted = True

        async def save_identity(self, platform, account, user):
            assert user["unique_id"] == account

    class Crawler:
        handles_douyin_signing = True

        def __init__(self, handle):
            self.handle = handle
            self._author_identity_repository = Repository()
            self._author_identity_cache = {}
            self.calls = []

        async def _get_json(self, url, *, params, headers):
            self.calls.append(url)
            if "/profile/other/" in url:
                assert params["sec_user_id"] == SEC_UID
                return {"status_code": 0, "user": {"sec_uid": SEC_UID, "unique_id": self.handle}}
            return {"status_code": 0, "user_list": []}

    async def run():
        valid = Crawler("public.handle")
        assert await resolve_douyin(valid, "public.handle", {}, {}) == SEC_UID
        assert await resolve_douyin(valid, "public.handle", {}, {}) == SEC_UID
        assert len(valid.calls) == 1
        stale = Crawler("renamed.handle")
        with pytest.raises(AuthorIdentityError):
            await resolve_douyin(stale, "public.handle", {}, {})
        assert stale._author_identity_repository.deleted
        assert not stale._author_identity_cache

    asyncio.run(run())


def test_douyin_transient_retries_keep_working_protocol_transport(monkeypatch):
    from contextlib import asynccontextmanager
    from app.crawlers.http_client import PlatformCrawlerError
    from app.models.author import AuthorPage
    from app.services import author_service
    from app.services.engagement_service import EngagementService

    @asynccontextmanager
    async def scope():
        yield

    async def cooldown(platform):
        pass

    seen = []
    async def handler(transport, request, state):
        assert isinstance(transport, douyin_session.DouyinProtocolTransport)
        seen.append(transport)
        if len(seen) < 3:
            raise PlatformCrawlerError("engagement request failed")
        return AuthorPage(uid=request.uid, media_name=request.media_name)

    monkeypatch.setitem(author_service.HANDLERS, "douyin", handler)

    async def run():
        client = SimpleNamespace(isolated_session_scope=scope, lease_scope=scope,
                                 invalidate_active_lease=lambda reason: None)
        parent = SimpleNamespace(client=client, _wait_for_circuit_cooldown=cooldown)
        service = author_service.AuthorService(EngagementService(parent, author_douyin_cookie="UIFID_TEMP=test", author_guest_only=False))
        request = AuthorRequest(author_id="public.handle", media_name="douyin")
        async with service.discovery_session(request) as source:
            await source.fetch_page(request)
        assert len(seen) == 3 and len({id(item) for item in seen}) == 3

    asyncio.run(run())


def test_invisible_profile_requires_fresh_reference_work_evidence():
    from app.crawlers.authors.identity import resolve_douyin, AuthorIdentityError

    class Repository:
        async def get_identity(self, *args):
            return {"user": {"sec_uid": SEC_UID}, "source_work_id": "123"}

        async def delete_identity(self, *args):
            pass

        async def save_identity(self, platform, account, user):
            assert user["unique_id"] == account

    class Crawler:
        handles_douyin_signing = True

        def __init__(self, handle):
            self.handle = handle
            self._author_identity_repository = Repository()

        async def _get_json(self, url, **kwargs):
            if "/profile/other/" in url:
                return {"status_code": 1}
            if "/aweme/detail/" in url:
                assert kwargs["params"]["aweme_id"] == "123"
                return {"status_code": 0, "aweme_detail": {
                    "aweme_id": "123", "author": {"sec_uid": SEC_UID, "unique_id": self.handle}
                }}
            return {"status_code": 0, "user_list": []}

    assert asyncio.run(resolve_douyin(Crawler("public.handle"), "public.handle", {}, {})) == SEC_UID
    with pytest.raises(AuthorIdentityError):
        asyncio.run(resolve_douyin(Crawler("other.handle"), "public.handle", {}, {}))
