"""Author-only sessions, scoped to the platform and the actual request host."""

from urllib.parse import urlsplit

from app.crawlers.engagement import EngagementCrawler


DOMAINS = {
    "kuaishou": ("kuaishou.com",),
    "weibo": ("weibo.com", "weibo.cn"),
    "bilibili": ("bilibili.com",),
    "xiaohongshu": ("xiaohongshu.com",),
    "toutiao": ("toutiao.com",),
}


def platform_for_url(url):
    host = urlsplit(url).hostname or ""
    for platform, domains in DOMAINS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    return None


class AuthorCrawler(EngagementCrawler):
    def __init__(self, *, author_session_store=None, **kwargs):
        super().__init__(**kwargs)
        self.author_session_store = author_session_store

    def browser_state(self, platform):
        if self.author_session_store and platform in DOMAINS:
            return self.author_session_store.scoped_state(platform, DOMAINS[platform])
        return {"cookies": [], "origins": []}

    def cookie_for_url(self, url):
        platform = platform_for_url(url)
        if not platform:
            return ""
        configured = self.platform_cookies.get(platform, "")
        if configured:
            return configured
        parsed = urlsplit(url)
        values = {}
        for cookie in sorted(self.browser_state(platform)["cookies"], key=lambda c: len(c.get("path", "/"))):
            domain = cookie["domain"]
            host = parsed.hostname or ""
            matches = host == domain.lstrip(".") or (domain.startswith(".") and host.endswith(domain))
            path = cookie.get("path", "/")
            request_path = parsed.path or "/"
            path_matches = request_path == path or request_path.startswith(path.rstrip("/") + "/")
            if matches and path_matches and (not cookie.get("secure") or parsed.scheme == "https"):
                values[cookie["name"]] = cookie["value"]
        return "; ".join(f"{k}={v}" for k, v in values.items())

    def _platform_cookie(self, platform):
        if platform in DOMAINS:
            return self.cookie_for_url("https://www." + DOMAINS[platform][0] + "/")
        return super()._platform_cookie(platform)

    def _session_headers(self, url, headers):
        headers = dict(headers or {})
        cookie = self.cookie_for_url(url)
        if cookie and not any(k.lower() == "cookie" for k in headers):
            headers["Cookie"] = cookie
        return headers

    async def _get_response(self, url, *, headers=None, **kwargs):
        if not kwargs.get("discard_cookies"):
            headers = self._session_headers(url, headers)
        return await super()._get_response(url, headers=headers, **kwargs)

    async def _post_response(self, url, *, headers=None, **kwargs):
        return await super()._post_response(url, headers=self._session_headers(url, headers), **kwargs)
