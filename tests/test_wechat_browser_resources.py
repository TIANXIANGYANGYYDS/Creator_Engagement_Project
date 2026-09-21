import asyncio
from types import SimpleNamespace

import pytest

from app.crawlers import wechat_browser_resources as assets


class Context:
    async def route(self, pattern, handler):
        self.pattern, self.handler = pattern, handler

    async def unroute(self, pattern, handler):
        assert (pattern, handler) == (self.pattern, self.handler)
        self.removed = True


class Route:
    def __init__(self, url="https://res.wx.qq.com/mmbizwap/moon.js", method="GET", kind="script"):
        self.request = SimpleNamespace(url=url, method=method, resource_type=kind,
                                       headers={"user-agent": "browser", "cookie": "secret", "authorization": "secret"})
        self.continued = False
        self.fulfilled = None

    async def continue_(self):
        self.continued = True

    async def fulfill(self, **kwargs):
        self.fulfilled = kwargs


def fake_client(monkeypatch, failure=None):
    calls = []
    from contextlib import asynccontextmanager

    class Client:
        def __init__(self, **settings):
            self.settings = settings
            self.closed = False
            calls.append(self)

        @asynccontextmanager
        async def lease_scope(self):
            yield

        async def get(self, url, **kwargs):
            self.request = (url, kwargs)
            if failure:
                raise failure
            return SimpleNamespace(status_code=200, content=b"official-script", headers={"content-type": "application/javascript", "set-cookie": "secret"})

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(assets, "CurlAsyncHttpClient", Client)
    return calls


def test_static_recovery_uses_exact_browser_proxy_and_no_credentials(monkeypatch):
    calls = fake_client(monkeypatch)

    async def run():
        context, route = Context(), Route()
        proxy = {"https": "http://proxy:80"}
        async with assets.wechat_static_resources(context, proxy):
            await context.handler(route)
            assert await calls[0].settings["proxy_provider"].get_requests_proxies() == proxy
            assert calls[0].settings["proxy_mode"] == "required"
            assert calls[0].request[1] == {"headers": {"User-Agent": "browser"}, "discard_cookies": True}
            assert route.fulfilled == {"status": 200, "body": b"official-script", "content_type": "application/javascript"}
            assert not route.continued
        assert context.removed and calls[0].closed
    asyncio.run(run())


@pytest.mark.parametrize("url,method,kind", [
    ("https://res.wx.qq.com.evil.test/moon.js", "GET", "script"),
    ("http://res.wx.qq.com/moon.js", "GET", "script"),
    ("https://res.wx.qq.com:444/moon.js", "GET", "script"),
    ("https://mp.weixin.qq.com/s", "GET", "document"),
    ("https://res.wx.qq.com/api", "POST", "script"),
    ("https://res.wx.qq.com/api", "GET", "fetch"),
])
def test_navigation_and_api_requests_stay_in_browser(monkeypatch, url, method, kind):
    calls = fake_client(monkeypatch)

    async def run():
        context, route = Context(), Route(url, method, kind)
        async with assets.wechat_static_resources(context, {"https": "http://proxy:80"}):
            await context.handler(route)
            assert route.continued and route.fulfilled is None
            assert not hasattr(calls[0], "request")
    asyncio.run(run())


def test_failed_asset_keeps_normal_browser_path(monkeypatch):
    calls = fake_client(monkeypatch, TimeoutError())

    async def run():
        context, route = Context(), Route()
        async with assets.wechat_static_resources(context, {"https": "http://proxy:80"}):
            await context.handler(route)
            assert route.continued and route.fulfilled is None
        assert calls[0].closed
    asyncio.run(run())


def test_cancellation_closes_static_session(monkeypatch):
    calls = fake_client(monkeypatch, asyncio.CancelledError())

    async def run():
        context = Context()
        with pytest.raises(asyncio.CancelledError):
            async with assets.wechat_static_resources(context, {"https": "http://proxy:80"}):
                await context.handler(Route())
        assert context.removed and calls[0].closed
    asyncio.run(run())


def test_no_proxy_does_not_install_recovery(monkeypatch):
    calls = fake_client(monkeypatch)

    async def run():
        context = Context()
        async with assets.wechat_static_resources(context, None):
            assert not hasattr(context, "handler")
        assert calls == []
    asyncio.run(run())
