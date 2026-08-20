from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable, Literal

from app.core.config import Settings
from app.crawlers.browser_fallback import BrowserFallback, BrowserFallbackSettings
from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platform_session import PlatformSessionStore
from app.crawlers.proxy_provider import AsyncDailiProxyPool, AsyncProxyProvider
from app.models.engagement import CommentPageResult, EngagementResult, InteractionResult


class EngagementService:
    """Application boundary for single and batch URL collection."""

    def __init__(
        self,
        crawler: EngagementCrawler,
        *,
        proxy_provider: AsyncProxyProvider | None = None,
    ) -> None:
        self.crawler = crawler
        self.proxy_provider = proxy_provider

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
        browser_fallback = None
        if settings.browser_fallback_enabled:
            browser_fallback = BrowserFallback(
                settings=BrowserFallbackSettings(
                    timeout_seconds=settings.browser_timeout_seconds,
                    challenge_wait_seconds=settings.browser_challenge_wait_seconds,
                    headless=settings.browser_headless,
                    profile_dir=Path(settings.browser_profile_dir),
                ),
                proxy_provider=provider,
                cookies=settings.creator_engagement_cookie.get_secret_value(),
            )
        crawler = EngagementCrawler(
            timeout_seconds=settings.request_timeout_seconds,
            cookies=settings.creator_engagement_cookie.get_secret_value(),
            aidata_api_key=settings.aidata_api_key.get_secret_value(),
            aidata_base_url=settings.aidata_base_url,
            proxy_provider=provider,
            proxy_mode=active_mode,
            browser_fallback=browser_fallback,
            session_store=PlatformSessionStore(Path(settings.platform_session_dir)),
        )
        return cls(crawler, proxy_provider=provider)

    async def fetch(self, url: str, *, comment_limit: int = 20) -> EngagementResult:
        return await self.crawler.fetch(url, comment_limit=comment_limit)

    async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
        return await self.crawler.fetch_interactions(url, media_name)

    async def fetch_comments(
        self,
        url: str,
        media_name: str,
        page: int,
    ) -> CommentPageResult:
        return await self.crawler.fetch_comments(url, media_name, page)

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
        await self.crawler.aclose()
        if self.proxy_provider is not None:
            await self.proxy_provider.close()
