from __future__ import annotations

import asyncio
from typing import Iterable, Literal

from app.core.config import Settings
from app.crawlers.engagement import EngagementCrawler
from app.crawlers.proxy_provider import AsyncDailiProxyPool, AsyncProxyProvider
from app.models.engagement import EngagementResult


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
        crawler = EngagementCrawler(
            timeout_seconds=settings.request_timeout_seconds,
            cookies=settings.creator_engagement_cookie.get_secret_value(),
            proxy_provider=provider,
            proxy_mode=active_mode,
        )
        return cls(crawler, proxy_provider=provider)

    async def fetch(self, url: str, *, comment_limit: int = 20) -> EngagementResult:
        return await self.crawler.fetch(url, comment_limit=comment_limit)

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
