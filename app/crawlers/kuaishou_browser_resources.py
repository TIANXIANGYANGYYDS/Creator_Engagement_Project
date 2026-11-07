"""Carry the browser's guest initialization over its existing proxy lease."""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from app.crawlers.http_client import CurlAsyncHttpClient
from app.crawlers.wechat_browser_resources import _PinnedProxy


@asynccontextmanager
async def kuaishou_guest_transport(context, proxy_mapping):
    """Recover verified CONNECT failures; the site's SDK still builds each body.

    Only the observed guest bootstrap/report endpoints are forwarded. Business
    requests, account login and CAPTCHA verification stay in the browser.
    """
    if not proxy_mapping:
        yield
        return
    client = CurlAsyncHttpClient(timeout_seconds=10, connect_timeout_seconds=3,
        headers={}, proxy_provider=_PinnedProxy(proxy_mapping), proxy_mode="required")
    pattern = "https://gdfp.gifshow.com/**"
    gate = asyncio.Semaphore(2)

    async def handle(route):
        request = route.request
        target = urlsplit(request.url)
        if (target.scheme != "https" or target.hostname != "gdfp.gifshow.com"
                or target.port not in (None, 443)
                or target.path not in {"/s/w/c", "/s/u/v", "/p/z/s"}
                or request.method not in {"POST", "OPTIONS"}
                or request.resource_type not in {"xhr", "fetch"}):
            await route.continue_()
            return
        body = request.post_data_buffer
        if body is not None and len(body) > 256 * 1024:
            await route.continue_()
            return
        headers = {k: v for k, v in request.headers.items() if k.lower() in {
            "user-agent", "content-type", "origin", "referer", "accept",
            "access-control-request-method", "access-control-request-headers",
        }}
        response = None
        try:
            async with asyncio.timeout(10), gate, client.lease_scope():
                response = await client._request(request.method, request.url,
                    headers=headers, data=body, discard_cookies=True)
        except Exception:
            pass
        try:
            if response is not None and 200 <= response.status_code < 300 and len(response.content) <= 2 * 1024**2:
                await route.fulfill(status=response.status_code, body=response.content,
                    headers={k: v for k, v in response.headers.items() if k.lower() in {
                        "content-type", "access-control-allow-origin",
                        "access-control-allow-credentials", "access-control-allow-methods",
                        "access-control-allow-headers", "access-control-max-age",
                    }})
            else:
                await route.continue_()
        except Exception:
            # A completed/closed route must not be handled a second time.
            pass

    try:
        await context.route(pattern, handle)
        yield
    finally:
        try:
            await context.unroute(pattern, handle)
        finally:
            await client.aclose()
