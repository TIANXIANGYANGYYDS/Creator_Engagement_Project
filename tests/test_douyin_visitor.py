from __future__ import annotations

import asyncio
import json
from time import monotonic
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app.crawlers.douyin_visitor import DouyinVisitor, DouyinVisitorSessions
from app.crawlers.engagement import EngagementCrawler
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.douyin_abogus_v2 import DEFAULT_BROWSER_INFO


PROFILE = {
    "user_agent": "Mozilla/5.0 Firefox/152.0",
    "browser_info": DEFAULT_BROWSER_INFO,
    "params": {"aid": "6383"},
}


def test_visitor_reuse_is_bound_to_proxy_and_expiry(monkeypatch):
    class Client:
        proxy = "http://proxy-one:80"

        async def active_proxy_mapping(self):
            return {"https": self.proxy}

    async def run():
        client = Client()
        sessions = DouyinVisitorSessions(client)
        minted = []

        async def bootstrap(referer):
            await asyncio.sleep(0)
            visitor = DouyinVisitor({"UIFID_TEMP": str(len(minted))}, PROFILE, monotonic() + 10)
            minted.append(visitor)
            return visitor

        monkeypatch.setattr(sessions, "_bootstrap", bootstrap)
        first, duplicate = await asyncio.gather(sessions.get("url"), sessions.get("url"))
        assert first is duplicate
        assert len(minted) == 1
        client.proxy = "http://proxy-two:80"
        second = await sessions.get("url")
        assert second is not first
        client.proxy = "http://proxy-one:80"
        assert await sessions.get("url") is first
        first.expires_at = 0
        assert await sessions.get("url") is not first
        assert len(minted) == 3

    asyncio.run(run())


def test_bootstrap_is_anonymous_and_closes_browser_on_failure(monkeypatch):
    from app.crawlers.authors import browser

    instances = []

    class Browser:
        def __init__(self, guest, semaphore, **kwargs):
            assert guest.platform_cookies == {}
            assert not hasattr(guest, "browser_state")
            assert kwargs["wait_for_douyin_sdk"] is False
            self.closed = False
            instances.append(self)

        async def open(self, url):
            raise RuntimeError("transport failure containing sensitive details")

        async def close(self):
            self.closed = True

    monkeypatch.setattr(browser, "BrowserTransport", Browser)

    async def run():
        sessions = DouyinVisitorSessions(SimpleNamespace())
        with pytest.raises(PlatformCrawlerError, match="初始化失败") as exc:
            await sessions._bootstrap("https://www.douyin.com/")
        assert "sensitive" not in str(exc.value)
        assert instances[0].closed
        assert not sessions._sessions

    asyncio.run(run())


def test_bootstrap_expiry_does_not_outlive_proxy_and_cancellation_closes(monkeypatch):
    from app.crawlers.authors import browser

    instances = []

    class Browser:
        def __init__(self, *args, **kwargs):
            self.page = self
            self.context = self
            self.closed = False
            instances.append(self)

        async def open(self, url):
            if url == "cancel":
                raise asyncio.CancelledError

        async def cookies(self, url):
            return [{"name": "UIFID_TEMP", "value": "visitor"}]

        async def evaluate(self, script):
            return PROFILE

        async def close(self):
            self.closed = True

    monkeypatch.setattr(browser, "BrowserTransport", Browser)

    async def run():
        sessions = DouyinVisitorSessions(SimpleNamespace(active_lease_remaining_seconds=lambda: 25))
        visitor = await sessions._bootstrap("url")
        assert 0 < visitor.expires_at - monotonic() <= 15
        assert instances[-1].closed
        with pytest.raises(asyncio.CancelledError):
            await sessions._bootstrap("cancel")
        assert instances[-1].closed

    asyncio.run(run())


@pytest.mark.parametrize("returned_id", ["7694332600140252142", "unrelated", None])
def test_detail_uses_current_visitor_signature_and_rejects_other_works(returned_id):
    visitor = DouyinVisitor(
        {"UIFID_TEMP": "visitor", "ttwid": "guest", "msToken": "actual+/=", "s_v_web_id": "verify_current"},
        PROFILE, monotonic() + 10,
    )
    calls = []

    class Client:
        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            payload = {"status_code": 0, "aweme_detail": {
                "aweme_id": returned_id, "statistics": {"digg_count": 23, "comment_count": 2},
            }}
            return SimpleNamespace(status_code=200, text=json.dumps(payload), json=lambda: payload, cookies={})

    class Sessions:
        async def get(self, referer):
            return visitor

    crawler = EngagementCrawler(client=Client(), cookies="unrelated-global-cookie")
    crawler.douyin_visitor_sessions = Sessions()
    result = asyncio.run(crawler.fetch_interactions(
        "https://www.iesdouyin.com/share/video/7694332600140252142", "抖音",
    ))
    assert len(calls) == 1  # No ttwid registration or legacy Argus request.
    query = parse_qs(urlsplit(calls[0][0]).query)
    assert query["uifid"] == ["visitor"]
    assert query["msToken"] == ["actual+/="]
    assert query["fp"] == query["verifyFp"] == ["verify_current"]
    assert query["a_bogus"] and query["timestamp"] and query["x-secsdk-web-signature"]
    assert calls[0][1]["headers"]["User-Agent"] == PROFILE["user_agent"]
    assert "unrelated-global-cookie" not in calls[0][1]["headers"]["Cookie"]
    if returned_id == "7694332600140252142":
        assert result.stats.likes == 23
        assert result.coverage == "partial"
    else:
        assert result.coverage == "failed"
        assert result.stats.likes is None
        assert "ID" in result.reason
