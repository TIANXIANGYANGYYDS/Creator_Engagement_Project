"""Short-lived browser transport using the same leased IP as its protocol flow."""

from __future__ import annotations

import asyncio
import json
import re
from time import monotonic
from collections import deque
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit, quote

from app.crawlers.browser_fallback import _playwright_proxy
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.authors import douyin_session
from app.crawlers.authors.session import DOMAINS, platform_for_url


def profile_url(request):
    return {
        "douyin": f"https://www.douyin.com/user/{request.uid}"
        if request.uid.startswith("MS4wLj")
        else f"https://www.douyin.com/search/{quote(request.uid)}?type=user",
        "kuaishou": f"https://www.kuaishou.com/profile/{request.uid}",
        "weibo": f"https://weibo.com/u/{request.uid}",
        "bilibili": f"https://space.bilibili.com/{request.uid}/dynamic",
        "xiaohongshu": f"https://www.xiaohongshu.com/user/profile/{request.uid}"
        if re.fullmatch(r"[0-9a-f]{24}", request.uid)
        else f"https://www.xiaohongshu.com/search_result?keyword={quote(request.uid)}&type=51",
        "toutiao": f"https://www.toutiao.com/c/user/{request.uid}/"
        if request.uid.isdigit()
        else f"https://www.toutiao.com/c/user/token/{request.uid}/",
    }[request.media_name]


class BrowserTransport:
    def __init__(self, crawler, semaphore, *, headless=True, wait_for_douyin_sdk=True):
        self.crawler = crawler
        self.client = crawler.client
        self.semaphore = semaphore
        self.headless = headless
        self.responses = deque(maxlen=64)
        self.manager = None
        self.page = None
        self._tasks = set()
        self._acquired = False
        self.douyin_sdk_ready = False
        self.douyin_request_mode = "native"
        self.wait_for_douyin_sdk = wait_for_douyin_sdk

    async def open(self, url):
        from camoufox.async_api import AsyncCamoufox
        from camoufox.addons import DefaultAddons

        await self.semaphore.acquire()
        self._acquired = True
        try:
            mapping = await self.client.active_proxy_mapping()
            self.manager = AsyncCamoufox(
                headless=self.headless,
                os="windows",
                locale="zh-CN",
                proxy=_playwright_proxy(mapping),
                firefox_user_prefs={"network.proxy.type": 0} if platform_for_url(url) == "xiaohongshu" else {},
                geoip=False,
                block_webrtc=True,
                exclude_addons=[DefaultAddons.UBO],
            )
            self.browser = await self.manager.__aenter__()
            platform = platform_for_url(url)
            state = (
                self.crawler.browser_state(platform)
                if hasattr(self.crawler, "browser_state") else {"cookies": [], "origins": []}
            )
            context = await self.browser.new_context(storage_state=state)
            self.page = await context.new_page()
            if urlsplit(url).hostname == "www.douyin.com":
                await self.page.add_init_script(douyin_session.INIT_SCRIPT)
                configured = getattr(self.crawler, "_author_douyin_configured_cookie", "")
                cookies = []
                for part in configured.split(";"):
                    name, separator, value = part.strip().partition("=")
                    if separator and name and value:
                        cookies.append({"name": name, "value": value, "domain": ".douyin.com", "path": "/"})
                if cookies:
                    await self.page.context.add_cookies(cookies)
            if platform in DOMAINS:
                configured = self.crawler.platform_cookies.get(platform, "")
                cookies = []
                for part in configured.split(";"):
                    name, sep, value = part.strip().partition("=")
                    if sep and name and value:
                        cookies.extend(
                            {
                                "name": name,
                                "value": value,
                                "domain": "." + domain,
                                "path": "/",
                            }
                            for domain in DOMAINS[platform]
                        )
                if cookies:
                    await self.page.context.add_cookies(cookies)
            self.page.on("response", self._on_response)
            await self.navigate(url)
            return self
        except BaseException:
            await self.close()
            raise

    def _on_response(self, response):
        path = urlsplit(response.url).path
        if not any(
            part in path
            for part in (
                "/user_posted",
                "/profile/feed",
                "/profile/get",
                "/aweme/post/",
                "/feed/space",
                "/app/api/pc/feed/",
                "/api/pc/list/user/feed",
                "/search/",
                "/profile/other/",
                "/user/otherinfo",
                "/graphql",
            )
        ):
            return
        task = asyncio.create_task(self._capture(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture(self, response):
        try:
            value = await response.json()
            if isinstance(value, dict):
                self.responses.append((response.request, value))
        except Exception:
            pass

    async def navigate(self, url):
        navigation_failed = False
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            # SPA requests sometimes finish despite a document navigation timeout.
            navigation_failed = True
        if navigation_failed and not self.page.url.startswith("https://"):
            raise PlatformCrawlerError("平台页面未能建立 HTTPS 连接，请重试代理")
        await self.page.wait_for_timeout(1500)
        if urlsplit(url).hostname == "www.douyin.com":
            await self._prepare_douyin()

    async def _prepare_douyin(self):
        # Cookie initialization and SDK readiness are separate. Native signing
        # can serve a valid visitor even if the site's fetch hook is unavailable.
        self.douyin_sdk_ready = False
        deadline, delay = monotonic() + 12, 0.4
        while True:
            try:
                async with asyncio.timeout(2):
                    fields = await self.page.evaluate(douyin_session.PROBE_SCRIPT)
                self.douyin_sdk_ready = "a_bogus" in fields
                cookies = await self.page.context.cookies("https://www.douyin.com")
                names = {c["name"] for c in cookies if c["value"]}
                if names.intersection(douyin_session.VISITOR_COOKIES):
                    if (
                        not self.wait_for_douyin_sdk
                        or self.douyin_sdk_ready
                        or monotonic() >= deadline
                    ):
                        return
            except Exception as exc:
                if monotonic() >= deadline:
                    raise PlatformBlockedError("抖音页面会话初始化未完成") from exc
            if monotonic() >= deadline:
                raise PlatformBlockedError("抖音访客会话尚未建立：缺少 UIFID")
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 2)

    async def captured(self, paths, *, cursor_key, cursor, uid_key=None, uid=None):
        for _ in range(3):
            for request, payload in list(self.responses):
                if not any(path in urlsplit(request.url).path for path in paths):
                    continue
                params = {
                    k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()
                }
                try:
                    params.update(request.post_data_json or {})
                    if isinstance(params.get("variables"), dict):
                        params.update(params["variables"])
                except Exception:
                    pass
                if uid_key and str(params.get(uid_key, "")) != uid:
                    continue
                if str(params.get(cursor_key) or "") != str(cursor or ""):
                    continue
                return payload
            await self.page.mouse.wheel(0, 1800)
            await self.page.wait_for_timeout(1200)
        return None

    async def cookie_header(self, origin):
        return "; ".join(
            f"{c['name']}={c['value']}" for c in await self.page.context.cookies(origin)
        )

    async def _get_json(self, url, *, params=None, headers=None, **kwargs):
        return await self._fetch("GET", url, params=params, headers=headers)

    async def _post_json(self, url, *, data=None, headers=None, **kwargs):
        return await self._fetch("POST", url, data=data, headers=headers)

    async def _fetch(self, method, url, *, params=None, data=None, headers=None):
        if method == "GET" and urlsplit(url).hostname == "www.douyin.com":
            return await self._douyin_json(url, params or {})
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        safe_headers = {
            k: v
            for k, v in (headers or {}).items()
            if k.lower() not in {"cookie", "referer", "user-agent", "origin"}
        }
        # Browser SDK interceptors attach current signatures; nothing is hardcoded.
        try:
            result = await self.page.evaluate(
                """async ({method, url, body, headers}) => {
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), 20000);
              try {
                const response = await fetch(url, {method, body: method === 'GET' ? undefined : body,
                  headers, credentials: 'include', signal: controller.signal});
                return {status: response.status, text: await response.text()};
              } finally { clearTimeout(timer); }
            }""",
                {"method": method, "url": url, "body": data, "headers": safe_headers},
            )
        except Exception as exc:
            raise PlatformCrawlerError("浏览器接口连接失败或超时，请重试代理及平台会话") from exc
        if result["status"] >= 400:
            raise PlatformCrawlerError(f"浏览器接口 HTTP {result['status']}")
        try:
            payload = json.loads(result["text"])
        except ValueError as exc:
            raise PlatformCrawlerError(
                "浏览器接口未返回 JSON，可能需要验证或登录"
            ) from exc
        if not isinstance(payload, dict):
            raise PlatformCrawlerError("浏览器接口数据结构不可用")
        return payload

    async def _douyin_json(self, url, params):
        profile = await self.page.evaluate(douyin_session.PROFILE_SCRIPT)
        cookies = {
            c["name"]: c["value"]
            for c in await self.page.context.cookies("https://www.douyin.com")
        }
        native_url = douyin_session.signed_url(url, params, cookies, profile)
        sdk_params = {**profile["params"], **params}
        for key in douyin_session.SIGNATURE_FIELDS | {"msToken"}:
            sdk_params.pop(key, None)
        if cookies.get("msToken") or profile.get("token"):
            sdk_params["msToken"] = cookies.get("msToken") or profile["token"]
        sdk_url = url + "?" + urlencode(sdk_params)
        modes = [self.douyin_request_mode]
        if self.douyin_sdk_ready:
            modes.append("sdk" if modes[0] == "native" else "native")
        for mode in modes:
            try:
                result = await self.page.evaluate(
                    """async ({url, native}) => {
                      const controller = new AbortController();
                      const timer = setTimeout(() => controller.abort(), 15000);
                      try {
                        const send = native ? window.__creatorDouyin.nativeFetch : window.fetch.bind(window);
                        const r = await send(url, {credentials: 'include', signal: controller.signal});
                        return {status: r.status, text: await r.text()};
                      } finally { clearTimeout(timer); }
                    }""",
                    {"url": native_url if mode == "native" else sdk_url, "native": mode == "native"},
                )
            except Exception as exc:
                raise PlatformCrawlerError("抖音请求超时或连接失败，可从当前游标续跑") from exc
            if result["status"] in {401, 403, 412, 429}:
                failure = PlatformBlockedError(f"抖音会话请求受限 HTTP {result['status']}")
                continue
            if result["status"] != 200:
                raise PlatformCrawlerError(f"抖音接口 HTTP {result['status']}")
            try:
                payload = json.loads(result["text"])
            except ValueError:
                failure = PlatformBlockedError("抖音接口返回空包或非 JSON，不能视作列表结束")
                continue
            if not isinstance(payload, dict) or payload.get("status_code") != 0:
                failure = PlatformBlockedError("抖音接口未返回有效业务响应，需更新会话")
                continue
            expected_list = {
                "/aweme/v1/web/aweme/post/": "aweme_list",
                "/aweme/v1/web/discover/search/": "user_list",
            }.get(urlsplit(url).path)
            if expected_list and not isinstance(payload.get(expected_list), list):
                failure = PlatformBlockedError("抖音响应缺少列表数据，不能视作列表结束")
                continue
            self.douyin_request_mode = mode
            return payload
        raise failure

    async def _get_response(self, url, **kwargs):
        current, target = urlsplit(self.page.url), urlsplit(url)
        if (current.hostname, current.path.rstrip("/")) != (
            target.hostname,
            target.path.rstrip("/"),
        ):
            await self.navigate(url)
        try:
            await self.page.locator("body").wait_for(state="attached", timeout=8000)
        except Exception:
            pass
        return SimpleNamespace(text=await self.page.content())

    async def close(self):
        if self.page:
            self.page.remove_listener("response", self._on_response)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            if self.manager:
                await self.manager.__aexit__(None, None, None)
        finally:
            self.manager = None
            self.page = None
            if self._acquired:
                self.semaphore.release()
                self._acquired = False
