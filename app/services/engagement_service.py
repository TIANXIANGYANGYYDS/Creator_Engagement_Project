from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

from app.core.config import Settings
from app.crawlers.browser_fallback import BrowserFallback, BrowserFallbackSettings
from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platform_session import PlatformSessionStore
from app.crawlers.platforms.registry import normalize_media_name
from app.crawlers.proxy_provider import (
    AsyncDailiProxyPool,
    AsyncProxyProvider,
    AsyncRequestRateLimiter,
)
from app.models.engagement import (
    CommentPageResult,
    EngagementPlatform,
    EngagementResult,
    InteractionResult,
)


CachedResult = InteractionResult | CommentPageResult
CacheKey = tuple[str, str, str, int]


@dataclass(frozen=True)
class PlatformTrafficPolicy:
    max_concurrency: int
    min_interval_seconds: float


ENTERPRISE_PLATFORM_POLICIES: dict[EngagementPlatform, PlatformTrafficPolicy] = {
    "xiaohongshu": PlatformTrafficPolicy(max_concurrency=1, min_interval_seconds=4),
    "kuaishou": PlatformTrafficPolicy(max_concurrency=1, min_interval_seconds=0.5),
    "wechat": PlatformTrafficPolicy(max_concurrency=1, min_interval_seconds=0.5),
}


class EngagementService:
    """Application boundary for single and batch URL collection."""

    def __init__(
        self,
        crawler: EngagementCrawler,
        *,
        proxy_provider: AsyncProxyProvider | None = None,
        collection_max_concurrency: int = 4,
        cache_ttl_seconds: float = 120,
        cache_max_entries: int = 1000,
        platform_policies: dict[EngagementPlatform, PlatformTrafficPolicy] | None = None,
    ) -> None:
        if collection_max_concurrency <= 0:
            raise ValueError("collection_max_concurrency must be greater than zero")
        if cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must not be negative")
        if cache_max_entries <= 0:
            raise ValueError("cache_max_entries must be greater than zero")
        self.crawler = crawler
        self.proxy_provider = proxy_provider
        self._collection_semaphore = asyncio.Semaphore(collection_max_concurrency)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_max_entries = cache_max_entries
        self._result_cache: OrderedDict[
            CacheKey, tuple[float, CachedResult]
        ] = OrderedDict()
        self._result_tasks: dict[CacheKey, asyncio.Task[CachedResult]] = {}
        self._cache_lock = asyncio.Lock()
        policies = platform_policies or {}
        for policy in policies.values():
            if policy.max_concurrency <= 0:
                raise ValueError("platform max_concurrency must be greater than zero")
            if policy.min_interval_seconds < 0:
                raise ValueError("platform min_interval_seconds must not be negative")
        self._platform_semaphores = {
            platform: asyncio.Semaphore(policy.max_concurrency)
            for platform, policy in policies.items()
        }
        self._platform_rate_limiters = {
            platform: AsyncRequestRateLimiter(1 / policy.min_interval_seconds)
            for platform, policy in policies.items()
            if policy.min_interval_seconds > 0
        }

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        proxy_mode: Literal["direct", "prefer", "required"] | None = None,
    ) -> "EngagementService":
        active_mode = proxy_mode or settings.proxy_mode
        provider: AsyncProxyProvider | None = None
        if active_mode == "required" and not settings.proxy_51_api_url.strip():
            raise ValueError("PROXY_MODE=required 时必须配置 PROXY_51_API_URL")
        if active_mode != "direct" and settings.proxy_51_api_url.strip():
            provider = AsyncDailiProxyPool(
                minutes=3,
                pool_size=settings.proxy_pool_size,
                max_concurrency_per_proxy=settings.proxy_max_concurrency,
                api_url=settings.proxy_51_api_url,
            )
        session_store = PlatformSessionStore(Path(settings.platform_session_dir))
        browser_fallback = None
        if settings.browser_fallback_enabled:
            browser_fallback = BrowserFallback(
                settings=BrowserFallbackSettings(
                    timeout_seconds=settings.browser_timeout_seconds,
                    challenge_wait_seconds=settings.browser_challenge_wait_seconds,
                    headless=settings.browser_headless,
                    max_concurrency=settings.browser_max_concurrency,
                    profile_dir=Path(settings.browser_profile_dir),
                    reset_guest_state_on_proxy_change=(
                        settings.browser_reset_guest_state_on_proxy_change
                    ),
                ),
                proxy_provider=provider,
                session_store=session_store,
                cookies=settings.creator_engagement_cookie.get_secret_value(),
            )
        crawler = EngagementCrawler(
            timeout_seconds=settings.request_timeout_seconds,
            cookies=settings.creator_engagement_cookie.get_secret_value(),
            proxy_provider=provider,
            proxy_mode=active_mode,
            browser_fallback=browser_fallback,
            session_store=session_store,
            max_protocol_attempts=(
                settings.protocol_max_attempts
                if settings.reliability_mode == "enterprise"
                else 1
            ),
            protocol_retry_base_seconds=settings.protocol_retry_base_seconds,
        )
        return cls(
            crawler,
            proxy_provider=provider,
            collection_max_concurrency=settings.collection_max_concurrency,
            cache_ttl_seconds=settings.engagement_cache_ttl_seconds,
            cache_max_entries=settings.engagement_cache_max_entries,
            platform_policies=(
                ENTERPRISE_PLATFORM_POLICIES
                if settings.reliability_mode == "enterprise"
                else None
            ),
        )

    async def fetch(self, url: str, *, comment_limit: int = 20) -> EngagementResult:
        async with self._collection_semaphore:
            return await self.crawler.fetch(url, comment_limit=comment_limit)

    async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
        platform = normalize_media_name(media_name)

        async def collect() -> CachedResult:
            return await self.crawler.fetch_interactions(url, media_name)

        result = await self._fetch_cached(
            ("interactions", platform, url, 0),
            collect,
        )
        assert isinstance(result, InteractionResult)
        return result

    async def fetch_comments(
        self,
        url: str,
        media_name: str,
        page: int,
    ) -> CommentPageResult:
        if page <= 0:
            raise ValueError("page must be greater than zero")
        platform = normalize_media_name(media_name)

        async def collect() -> CachedResult:
            return await self.crawler.fetch_comments(url, media_name, page)

        result = await self._fetch_cached(
            ("comments", platform, url, page),
            collect,
        )
        assert isinstance(result, CommentPageResult)
        return result

    async def _fetch_cached(
        self,
        key: CacheKey,
        collect: Callable[[], Awaitable[CachedResult]],
    ) -> CachedResult:
        now = monotonic()
        async with self._cache_lock:
            while self._result_cache:
                oldest_key, (expires_at, _) = next(iter(self._result_cache.items()))
                if expires_at > now:
                    break
                self._result_cache.pop(oldest_key)
            cached = self._result_cache.get(key)
            if cached is not None and cached[0] > now:
                self._result_cache.move_to_end(key)
                return cached[1].model_copy(deep=True)
            task = self._result_tasks.get(key)
            if task is None:
                task = asyncio.create_task(self._collect_cached(key[1], collect))
                self._result_tasks[key] = task
        try:
            result = await asyncio.shield(task)
        except Exception:
            async with self._cache_lock:
                if self._result_tasks.get(key) is task:
                    self._result_tasks.pop(key, None)
            raise
        async with self._cache_lock:
            if self._result_tasks.get(key) is task:
                self._result_tasks.pop(key, None)
            if self._cache_ttl_seconds > 0 and self._is_cacheable_result(result):
                self._result_cache[key] = (
                    monotonic() + self._cache_ttl_seconds,
                    result.model_copy(deep=True),
                )
                self._result_cache.move_to_end(key)
                while len(self._result_cache) > self._cache_max_entries:
                    self._result_cache.popitem(last=False)
        return result.model_copy(deep=True)

    async def _collect_cached(
        self,
        platform: str,
        collect: Callable[[], Awaitable[CachedResult]],
    ) -> CachedResult:
        platform_semaphore = self._platform_semaphores.get(platform)
        if platform_semaphore is None:
            async with self._collection_semaphore:
                return await collect()
        async with platform_semaphore:
            limiter = self._platform_rate_limiters.get(platform)
            if limiter is not None:
                await limiter.acquire()
            async with self._collection_semaphore:
                return await collect()

    @staticmethod
    def _is_cacheable_result(result: CachedResult) -> bool:
        if result.coverage not in {"complete", "partial"}:
            return False
        if result.coverage == "complete":
            return True
        if isinstance(result, InteractionResult):
            return any(value is not None for value in result.stats.model_dump().values())
        if result.comments or result.total_comments == 0:
            return True
        return result.page > 1 and result.next_page is None and result.total_comments is not None

    async def fetch_many(
        self,
        urls: Iterable[str],
        *,
        comment_limit: int = 20,
        concurrency: int = 4,
    ) -> list[EngagementResult]:
        if concurrency <= 0:
            raise ValueError("concurrency must be greater than zero")
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_one(url: str) -> EngagementResult:
            async with semaphore:
                return await self.fetch(url, comment_limit=comment_limit)

        return list(await asyncio.gather(*(fetch_one(url) for url in urls)))

    async def aclose(self) -> None:
        tasks = list(self._result_tasks.values())
        self._result_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.crawler.aclose()
        if self.proxy_provider is not None:
            await self.proxy_provider.close()
