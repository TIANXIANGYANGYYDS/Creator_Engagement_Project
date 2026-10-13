from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from time import monotonic
from datetime import datetime, timezone

from app.crawlers.authors import (
    bilibili,
    douyin,
    kuaishou,
    sina_blog,
    toutiao,
    weibo,
    xiaohongshu,
)
from app.crawlers.authors.browser import BrowserTransport, profile_url
from app.crawlers.authors.douyin_session import DouyinProtocolTransport
from app.crawlers.authors.session import AuthorCrawler
from app.crawlers.authors.identity import AuthorIdentityError
from app.crawlers.authors.common import decode_cursor, AuthorCursorError, AuthorSessionError
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.models.author import AuthorPage, AuthorRequest, AuthorWork
from app.services.engagement_service import EngagementService
from app.services.status_alerts import StatusAlerts

logger = logging.getLogger(__name__)
ADAPTERS = {
    m.__name__.rsplit(".", 1)[-1]: m
    for m in (douyin, kuaishou, weibo, bilibili, xiaohongshu, toutiao, sina_blog)
}
HANDLERS = {
    name: partial(module.fetch_page, include_details=False)
    for name, module in ADAPTERS.items()
}
BROWSER_LISTS = {"douyin", "kuaishou", "xiaohongshu", "toutiao"}


class AuthorService:
    def __init__(
        self, service: EngagementService, *, page_timeout_seconds: float = 120,
        identity_repository=None,
    ):
        self.service = service
        self.status_alerts = getattr(service, "status_alerts", None) or StatusAlerts()
        self.page_timeout_seconds = page_timeout_seconds
        self.identity_repository = identity_repository
        # Reuse the existing global browser budget, including interaction crawling.
        fallback = getattr(service.crawler, "browser_fallback", None)
        self.browser_slots = getattr(fallback, "_semaphore", None) or asyncio.Semaphore(
            2
        )
        self.headless = getattr(getattr(fallback, "settings", None), "headless", True)

    def _crawler(self):
        parent = self.service.crawler
        guest_only = getattr(self.service, "author_guest_only", True)
        enabled = not guest_only and bool(getattr(parent, "xiaohongshu_cookie_enabled", False))
        cookie = (
            getattr(parent, "platform_cookies", {}).get("xiaohongshu", "")
            if enabled
            else ""
        )
        crawler = AuthorCrawler(
            client=parent.client,
            strict_anonymous_mode=True,
            xiaohongshu_cookie_enabled=enabled,
            platform_cookies={
                **({} if guest_only else getattr(self.service, "author_platform_cookies", {})),
                **({"xiaohongshu": cookie} if cookie else {}),
            },
            author_session_store=None if guest_only else getattr(self.service, "author_session_store", None),
        )
        # This explicit author-only option never enters another platform's
        # protocol headers or the shared HTTP cookie jar.
        crawler._author_douyin_configured_cookie = getattr(
            self.service, "author_douyin_cookie", ""
        ) if not guest_only else ""
        crawler._author_guest_only = guest_only
        crawler._author_identity_repository = self.identity_repository
        return crawler

    @asynccontextmanager
    async def _slot(self, platform):
        if platform == "sina_blog":
            async with self.service._collection_semaphore:
                yield
        else:
            async with self.service.collection_slot(platform):
                yield

    @asynccontextmanager
    async def discovery_session(self, request):
        session = DiscoverySession(self, request)
        try:
            yield session
        finally:
            await session.close()

    async def fetch_page(
        self, request: AuthorRequest, cursor: str | None = None
    ) -> AuthorPage:
        async with asyncio.timeout(self.page_timeout_seconds):
            async with self.discovery_session(request) as source:
                page = await source.fetch_page(request, cursor)
            slots = asyncio.Semaphore(4)

            async def detail(work):
                if work.detail_status != "pending":
                    return work
                async with slots:
                    try:
                        return await self.fetch_detail(request, work)
                    except Exception as exc:
                        work.content_complete = False
                        work.detail_status = "failed"
                        work.error = (
                            "详情超时，可用任务接口续跑"
                            if isinstance(exc, TimeoutError)
                            else str(exc)[:500]
                        )
                        return work

            page.data = await asyncio.gather(*(detail(w) for w in page.data))
            return page

    async def fetch_detail(
        self, request: AuthorRequest, work: AuthorWork
    ) -> AuthorWork:
        with self.status_alerts.observe(request.media_name, "author_detail"):
            return await self._fetch_detail(request, work)

    async def _fetch_detail(
        self, request: AuthorRequest, work: AuthorWork
    ) -> AuthorWork:
        if not work.detail_context:
            if request.media_name == "weibo":
                work.detail_context = {"long_text": True}
            elif request.media_name == "bilibili":
                if work.work_id.startswith("BV"):
                    work.detail_context = {
                        "archive": {"bvid": work.work_id, "title": work.title}
                    }
                elif work.work_id.startswith("cv"):
                    work.detail_context = {
                        "article": {"id": work.work_id[2:], "title": work.title}
                    }
                else:
                    work.detail_context = {
                        "opus": {"title": work.title},
                        "dynamic_id": work.work_id.removeprefix("opus:"),
                    }
        client = self.service.crawler.client
        # Each work and each retry receives a private cookie jar and proxy lease.
        async with asyncio.timeout(self.page_timeout_seconds):
            for attempt in range(3):
                crawler = self._crawler()
                browser = None
                try:
                    async with (
                        self._slot(request.media_name),
                        client.isolated_session_scope(),
                        client.lease_scope(),
                    ):
                        try:
                            transport = crawler
                            if attempt == 2 and request.media_name in {
                                "kuaishou",
                                "xiaohongshu",
                                "bilibili",
                                "toutiao",
                            }:
                                browser = BrowserTransport(
                                    crawler, self.browser_slots, headless=self.headless
                                )
                                transport = await browser.open(work.url)
                            result = await ADAPTERS[request.media_name].fetch_detail(
                                transport, request, work.model_copy(deep=True)
                            )
                            if (
                                result.work_id != work.work_id
                                or result.author_uid != request.uid
                                or result.media_name != request.media_name
                            ):
                                raise PlatformCrawlerError("详情作品或作者身份不匹配")
                            result.fetched_at = datetime.now(timezone.utc)
                            result.content_complete = True
                            result.detail_status = "complete"
                            result.error = None
                            return result
                        except AuthorSessionError:
                            raise
                        except Exception:
                            client.invalidate_active_lease("author detail unusable")
                            raise
                        finally:
                            if browser:
                                await browser.close()
                except AuthorSessionError:
                    raise
                except Exception as exc:
                    if attempt == 2:
                        # Do not persist transport exception URLs, cookies or proxy credentials.
                        if isinstance(exc, PlatformCrawlerError):
                            raise
                        raise PlatformCrawlerError(
                            f"详情采集失败（{type(exc).__name__}），可续跑"
                        ) from exc
                finally:
                    await crawler.aclose()
        raise AssertionError("unreachable")


class DiscoverySession:
    """Keep list cookies/IP across pages; rotate before the endpoint expires."""

    def __init__(self, service, request):
        self.service = service
        self.request = request
        self.stack = None
        self.crawler = None
        self.browser = None
        self.transport = None
        self.browser_mode = request.media_name in BROWSER_LISTS
        guest_only = getattr(service.service, "author_guest_only", True)
        if not guest_only and request.media_name == "douyin" and getattr(service.service, "author_douyin_cookie", ""):
            self.browser_mode = False
        if not guest_only and request.media_name == "xiaohongshu" and getattr(
            service.service.crawler, "xiaohongshu_cookie_enabled", False
        ):
            self.browser_mode = False
        self.started = 0
        # Verified mappings survive an IP/browser rotation within this author
        # discovery. Untrusted client cursor fields never populate this cache.
        self.identity_cache = {}

    async def close(self):
        if self.stack:
            stack, self.stack = self.stack, None
            await stack.aclose()
        self.browser = None
        self.transport = None
        self.crawler = None

    async def _open(self):
        self.stack = AsyncExitStack()
        client = self.service.service.crawler.client
        await self.stack.enter_async_context(
            self.service._slot(self.request.media_name)
        )
        direct = getattr(client, "account_direct_scope", None)
        if self.request.media_name == "xiaohongshu" and callable(direct):
            await self.stack.enter_async_context(direct())
        await self.stack.enter_async_context(client.isolated_session_scope())
        await self.stack.enter_async_context(client.lease_scope())
        self.crawler = self.service._crawler()
        self.crawler._author_identity_cache = self.identity_cache
        self.stack.push_async_callback(self.crawler.aclose)
        self.started = monotonic()
        acquire = getattr(client, "active_proxy_mapping", None)
        if acquire:
            await acquire()
            remaining = client.active_lease_remaining_seconds()
            if remaining is not None and remaining < 40:
                client.invalidate_active_lease("author proxy nearing expiry")
                raise PlatformCrawlerError("代理剩余有效期不足，正在重新分配")
        if self.browser_mode:
            self.browser = BrowserTransport(
                self.crawler, self.service.browser_slots, headless=self.service.headless
            )
            self.stack.push_async_callback(self.browser.close)
            landing = profile_url(self.request)
            if self.request.media_name == "douyin":
                resolved = self.identity_cache.get(self.request.uid)
                if resolved:
                    landing = f"https://www.douyin.com/user/{resolved}"
            await self.browser.open(landing)
            self.browser._author_identity_cache = self.identity_cache
            self.browser._author_identity_repository = self.service.identity_repository
        elif self.request.media_name == "douyin":
            self.transport = DouyinProtocolTransport(
                self.crawler, self.crawler._author_douyin_configured_cookie
            )

    async def fetch_page(self, request, cursor=None):
        with self.service.status_alerts.observe(request.media_name, "author_list"):
            return await self._fetch_page(request, cursor)

    async def _fetch_page(self, request, cursor=None):
        state = decode_cursor(request, cursor)
        client = self.service.service.crawler.client
        async with asyncio.timeout(self.service.page_timeout_seconds):
            for attempt in range(3):
                try:
                    remaining = getattr(
                        client, "active_lease_remaining_seconds", lambda: None
                    )()
                    if self.stack and (
                        (remaining is not None and remaining < 40)
                        or monotonic() - self.started > 90
                    ):
                        if remaining is not None and remaining < 40:
                            client.invalidate_active_lease(
                                "author proxy nearing expiry"
                            )
                        await self.close()
                    if self.stack is None:
                        await self._open()
                    result = await HANDLERS[request.media_name](
                        self.browser or self.transport or self.crawler, request, state
                    )
                    if (
                        result.uid != request.uid
                        or result.media_name != request.media_name
                        or any(
                            w.author_uid != request.uid
                            or w.media_name != request.media_name
                            for w in result.data
                        )
                    ):
                        raise PlatformCrawlerError("作者列表身份不匹配")
                    return result
                except asyncio.CancelledError:
                    if self.crawler is not None:
                        client.invalidate_active_lease(
                            "author list interrupted or timed out"
                        )
                    raise
                except (AuthorCursorError, AuthorIdentityError, AuthorSessionError):
                    raise
                except Exception as exc:
                    logger.warning(
                        "author_attempt_failed platform=%s attempt=%s error_type=%s reason=%s",
                        request.media_name, attempt + 1, type(exc).__name__,
                        str(exc)[:200] if isinstance(exc, PlatformCrawlerError) else "transport failure",
                    )
                    client.invalidate_active_lease("author list unusable or blocked")
                    await self.close()
                    if attempt == 2:
                        if isinstance(exc, PlatformCrawlerError):
                            raise
                        raise PlatformCrawlerError(
                            f"作者列表采集失败（{type(exc).__name__}），可续跑"
                        ) from exc
                    if attempt == 1 and request.media_name in {
                        "douyin",
                        "bilibili",
                        "xiaohongshu",
                    } and (request.media_name != "douyin" or isinstance(exc, PlatformBlockedError)) and (
                        request.media_name != "bilibili" or state.get("source") == "dynamic" or "offset" in state
                    ):
                        self.browser_mode = True
                    logger.warning(
                        "author_page_retry platform=%s attempt=%s",
                        request.media_name,
                        attempt + 1,
                    )
        raise AssertionError("unreachable")
