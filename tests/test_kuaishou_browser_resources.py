import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.crawlers import kuaishou_browser_resources as resources


@pytest.mark.parametrize("path,method,allowed", [
    ("/s/w/c", "POST", True), ("/s/u/v", "POST", True),
    ("/p/z/s", "OPTIONS", True), ("/s/w/c", "GET", False),
    ("/login", "POST", False), ("/verify", "POST", False),
])
def test_guest_transport_keeps_original_body_and_proxy(monkeypatch, path, method, allowed):
    calls = []
    proxy = {"https": "http://proxy:80"}

    class Client:
        def __init__(self, **kwargs):
            self.provider = kwargs["proxy_provider"]
            self.closed = False
            calls.append(self)

        @asynccontextmanager
        async def lease_scope(self):
            yield

        async def _request(self, *args, **kwargs):
            self.request = (args, kwargs)
            assert await self.provider.get_requests_proxies() == proxy
            return SimpleNamespace(status_code=200, content=b'{"dataRsp":"fresh"}',
                headers={"content-type": "application/json", "access-control-allow-origin": "https://www.kuaishou.com"})

        async def aclose(self):
            self.closed = True

    class Context:
        async def route(self, pattern, handler):
            self.handler = handler

        async def unroute(self, pattern, handler):
            self.removed = True

    class Route:
        request = SimpleNamespace(url="https://gdfp.gifshow.com" + path,
            method=method, resource_type="xhr", post_data_buffer=b'{"data":"current-SDK-bytes"}',
            headers={"origin": "https://www.kuaishou.com", "content-type": "application/json",
                     "cookie": "unrelated=secret", "authorization": "secret"})
        fulfilled = None
        continued = False

        async def fulfill(self, **kwargs):
            self.fulfilled = kwargs

        async def continue_(self):
            self.continued = True

    monkeypatch.setattr(resources, "CurlAsyncHttpClient", Client)

    async def run():
        context, route = Context(), Route()
        async with resources.kuaishou_guest_transport(context, proxy):
            await context.handler(route)
        assert context.removed and calls[0].closed
        assert bool(route.fulfilled) is allowed
        if allowed:
            options = calls[0].request[1]
            assert options["data"] == route.request.post_data_buffer
            assert options["discard_cookies"] is True
            assert "cookie" not in options["headers"] and "authorization" not in options["headers"]
        else:
            assert route.continued and not hasattr(calls[0], "request")
    asyncio.run(run())
