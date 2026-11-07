"""Recover verified public CDN assets using the browser's existing proxy lease."""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from app.crawlers.http_client import CurlAsyncHttpClient


class _PinnedProxy:
    def __init__(self, mapping):
        self.mapping = mapping

    async def get_requests_proxies(self):
        return self.mapping


@asynccontextmanager
async def browser_static_resources(context, proxy_mapping, timeout_seconds=10, *, platform="wechat"):
    """Recover public page assets and visitor bootstrap scripts.

    Browser navigation, API calls and challenge responses remain in the browser.
    This handler forwards no Cookie or Authorization headers to the static CDN.
    """
    targets = {
        "wechat": ("res.wx.qq.com", "/"),
        "toutiao": ("unpkg.byted-static.com", "/byted-ucenter/ttwid-js/"),
        "kuaishou": ("p2-plat.wskwai.com", "/kos/nlav111422/pc-vision/js/"),
    }
    if not proxy_mapping or platform not in targets:
        yield
        return
    client = CurlAsyncHttpClient(
        timeout_seconds=min(10, timeout_seconds), connect_timeout_seconds=3,
        headers={}, proxy_provider=_PinnedProxy(proxy_mapping), proxy_mode="required",
    )
    gate = asyncio.Semaphore(2)
    host, prefix = targets[platform]
    pattern = f"https://{host}{prefix}**"

    async def handle(route):
        request = route.request
        target = urlsplit(request.url)
        if (target.scheme != "https" or target.hostname != host or not target.path.startswith(prefix)
                or target.port not in (None, 443) or request.method != "GET"
                or request.resource_type not in {"script", "stylesheet", "image", "font"}):
            await route.continue_()
            return
        response = None
        try:
            async with asyncio.timeout(min(10, timeout_seconds)), gate, client.lease_scope():
                response = await client.get(
                    request.url,
                    headers={"User-Agent": request.headers.get("user-agent", "")},
                    discard_cookies=True,
                )
        except Exception:
            # Navigation still has its normal path when asset recovery fails.
            pass
        try:
            if response is not None and response.status_code == 200 and len(response.content) <= 2 * 1024**2:
                await route.fulfill(status=200, body=response.content,
                    content_type=response.headers.get("content-type", "application/octet-stream"))
            else:
                await route.continue_()
        except Exception:
            # Navigation can close/consume a route while its fetch completes.
            # Never continue a route after fulfill has already been attempted.
            pass

    try:
        await context.route(pattern, handle)
        yield
    finally:
        try:
            await context.unroute(pattern, handle)
        finally:
            await client.aclose()


wechat_static_resources = browser_static_resources
