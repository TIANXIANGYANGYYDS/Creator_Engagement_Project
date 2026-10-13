"""Replay public collector requests inside a freshly established browser session."""
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.web_sources_common import json_or_jsonp


class BrowserPageContext:
    def __init__(self, page, target_url):
        self.page = page
        self.target_url = target_url

    async def _get_response(self, url, *, params=None, headers=None, **kwargs):
        if url.split("?", 1)[0].rstrip("/") == self.target_url.split("?", 1)[0].rstrip("/") and not params:
            return SimpleNamespace(text=await self.page.content(), status_code=200)
        request_url = url + (("&" if "?" in url else "?") + urlencode(params) if params else "")
        if urlsplit(url).netloc == urlsplit(self.page.url).netloc:
            data = await self.page.evaluate("""async (url) => {
                const response = await fetch(url, {credentials: 'include'});
                return {status: response.status, text: await response.text()};
            }""", request_url)
            status, text = data["status"], data["text"]
        else:
            response = await self.page.context.request.get(request_url, headers=headers or {}, timeout=15000)
            status, text = response.status, await response.text()
        if status in {401,403,429}:
            raise PlatformBlockedError(f"浏览器会话请求被限制: HTTP {status}")
        if status != 200:
            raise PlatformCrawlerError(f"浏览器请求失败: HTTP {status}")
        return SimpleNamespace(text=text, status_code=status)

    async def _get_json(self, url, *, params=None, headers=None):
        return json_or_jsonp((await self._get_response(url, params=params, headers=headers)).text)
