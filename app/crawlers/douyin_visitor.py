"""Short-lived, proxy-bound anonymous identities for Douyin's web API."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
from time import monotonic
from types import SimpleNamespace
from typing import Any

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError


logger = logging.getLogger(__name__)


@dataclass
class DouyinVisitor:
    cookies: dict[str, str]
    profile: dict[str, Any]
    expires_at: float

    def headers(self, referer: str) -> dict[str, str]:
        return {
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            "User-Agent": self.profile["user_agent"],
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        }


class DouyinVisitorSessions:
    """Mint at most one guest at a time; never copy an identity to another IP.

    Only the bootstrap needs a browser. API requests use the existing HTTP
    client, native signer and leased proxy. No account or disk Cookie is used.
    """

    def __init__(self, client, *, timeout_seconds=30, ttl_seconds=110):
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.ttl_seconds = ttl_seconds
        self._sessions: OrderedDict[tuple, DouyinVisitor] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, referer: str) -> DouyinVisitor:
        mapping = await self.client.active_proxy_mapping()
        key = tuple(sorted((mapping or {}).items()))
        visitor = self._sessions.get(key)
        if visitor is not None and visitor.expires_at > monotonic():
            return visitor
        async with self._lock:
            now = monotonic()
            for expired in [k for k, v in self._sessions.items() if v.expires_at <= now]:
                del self._sessions[expired]
            visitor = self._sessions.get(key)
            if visitor is not None:
                self._sessions.move_to_end(key)
                return visitor
            visitor = await self._bootstrap(referer)
            self._sessions[key] = visitor
            while len(self._sessions) > 32:
                self._sessions.popitem(last=False)
            return visitor

    async def _bootstrap(self, referer: str) -> DouyinVisitor:
        # Lazy imports keep the platform handler registry free of cycles.
        from app.crawlers.authors.browser import BrowserTransport
        from app.crawlers.authors.douyin_session import PROFILE_SCRIPT, VISITOR_COOKIES

        guest = SimpleNamespace(client=self.client, platform_cookies={})
        browser = BrowserTransport(
            guest, asyncio.Semaphore(1), wait_for_douyin_sdk=False,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await browser.open(referer)
                cookies = {
                    c["name"]: c["value"]
                    for c in await browser.page.context.cookies("https://www.douyin.com")
                }
                if not any(cookies.get(name) for name in VISITOR_COOKIES):
                    raise PlatformBlockedError("抖音访客会话未取得 UIFID")
                profile = await browser.page.evaluate(PROFILE_SCRIPT)
        except TimeoutError as exc:
            raise PlatformCrawlerError("抖音访客会话初始化超时") from exc
        except PlatformCrawlerError:
            raise
        except Exception as exc:
            raise PlatformCrawlerError("抖音访客会话初始化失败") from exc
        finally:
            await browser.close()
        ttl = self.ttl_seconds
        remaining = getattr(self.client, "active_lease_remaining_seconds", None)
        remaining_seconds = remaining() if callable(remaining) else None
        if remaining_seconds is not None:
            ttl = min(ttl, max(0, remaining_seconds - 10))
        if ttl <= 0:
            raise PlatformCrawlerError("抖音访客代理即将过期，需更新会话")
        logger.info("douyin_visitor_ready ttl_seconds=%.0f", ttl)
        return DouyinVisitor(cookies, profile, monotonic() + ttl)
