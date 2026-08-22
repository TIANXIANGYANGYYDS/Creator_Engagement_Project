"""Unified engagement facade and thin platform router.

Each media protocol lives in ``app.crawlers.platforms.<media>``.  This module
only validates the public request, routes it, and applies the optional browser
fallback when the protocol result is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.crawlers.http_client import (
    AsyncHttpClient,
    CurlAsyncHttpClient,
    PlatformBlockedError,
    PlatformCrawlerError,
)
from app.crawlers.platforms import PLATFORM_HANDLERS
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error
from app.crawlers.platforms.registry import (
    identify_url,
    validate_media_url,
)
from app.models.engagement import (
    CommentPageResult,
    EngagementPlatform,
    EngagementResult,
    InteractionResult,
)

if TYPE_CHECKING:
    from app.crawlers.browser_fallback import BrowserFallback
    from app.crawlers.platform_session import PlatformSessionStore


logger = logging.getLogger(__name__)


class EngagementCrawler:
    """Route the common API contract to one independent platform collector."""

    def __init__(
        self,
        *,
        client: AsyncHttpClient | None = None,
        timeout_seconds: float = 20,
        cookies: str = "",
        proxy_provider: Any | None = None,
        proxy_mode: str = "direct",
        browser_fallback: "BrowserFallback | None" = None,
        session_store: "PlatformSessionStore | None" = None,
        platform_cookies: dict[EngagementPlatform, str] | None = None,
        max_protocol_attempts: int = 1,
        protocol_retry_base_seconds: float = 1,
    ) -> None:
        if max_protocol_attempts <= 0:
            raise ValueError("max_protocol_attempts must be greater than zero")
        if protocol_retry_base_seconds < 0:
            raise ValueError("protocol_retry_base_seconds must not be negative")
        self._owns_client = client is None
        self.client = client or CurlAsyncHttpClient(
            timeout_seconds=timeout_seconds,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            proxy_provider=proxy_provider,
            proxy_mode=proxy_mode,
        )
        self.cookies = cookies
        self.browser_fallback = browser_fallback
        self.session_store = session_store
        self.platform_cookies = platform_cookies or {}
        self.max_protocol_attempts = max_protocol_attempts
        self.protocol_retry_base_seconds = protocol_retry_base_seconds

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()  # type: ignore[attr-defined]

    async def fetch(self, url: str, *, comment_limit: int = 20) -> EngagementResult:
        if comment_limit <= 0:
            raise ValueError("comment_limit must be greater than zero")
        return await self._fetch(
            url,
            limit=comment_limit,
            page=1,
            include_stats=True,
            include_comments=True,
        )

    async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
        result = await self._fetch(
            url,
            media_name=media_name,
            limit=COMMENT_PAGE_SIZE,
            page=1,
            include_stats=True,
            include_comments=False,
        )
        return InteractionResult.model_validate(
            result.model_dump(exclude={"comments", "next_cursor"})
        )

    async def fetch_comments(self, url: str, media_name: str, page: int) -> CommentPageResult:
        if page <= 0:
            raise ValueError("page must be greater than zero")
        result = await self._fetch(
            url,
            media_name=media_name,
            limit=COMMENT_PAGE_SIZE,
            page=page,
            include_stats=False,
            include_comments=True,
        )
        return CommentPageResult.model_validate({
            **result.model_dump(exclude={"stats", "next_cursor"}),
            "page": page,
            "next_page": page + 1 if result.next_cursor is not None else None,
            "total_comments": result.stats.comments,
        })

    async def _fetch(
        self,
        url: str,
        *,
        media_name: str | None = None,
        limit: int,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        platform, work_id = (
            validate_media_url(url, media_name) if media_name else identify_url(url)
        )
        if not work_id:
            raise ValueError(f"cannot extract {platform} content id from URL")

        handler = PLATFORM_HANDLERS.get(platform)
        if handler is None:
            result = result_error(
                platform,
                url,
                work_id,
                "unsupported",
                f"{platform} protocol collector is not registered",
            )
        else:
            result = await self._run_protocol_handler(
                handler,
                url,
                platform,
                work_id,
                limit=limit,
                page=page,
                include_stats=include_stats,
                include_comments=include_comments,
            )

        if self._should_use_browser_fallback(
            result,
            include_stats=include_stats,
            include_comments=include_comments,
        ):
            browser_result = await self._run_browser_fallback(
                url,
                platform,
                work_id,
                page=page,
                limit=limit,
                include_stats=include_stats,
                include_comments=include_comments,
            )
            if browser_result is not None:
                browser_result.protocol_attempts = result.protocol_attempts
                return browser_result
        return result

    async def _run_protocol_handler(
        self,
        handler: Any,
        url: str,
        platform: EngagementPlatform,
        work_id: str,
        *,
        limit: int,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        result: EngagementResult | None = None
        for attempt in range(1, self.max_protocol_attempts + 1):
            lease_scope = getattr(self.client, "lease_scope", None)
            if callable(lease_scope):
                async with lease_scope():
                    result = await handler(
                        self,
                        url,
                        work_id,
                        limit,
                        page=page,
                        include_stats=include_stats,
                        include_comments=include_comments,
                    )
                    if self._should_retry_protocol(
                        result,
                        include_stats=include_stats,
                        include_comments=include_comments,
                    ):
                        invalidate = getattr(self.client, "invalidate_active_lease", None)
                        if callable(invalidate):
                            invalidate(
                                f"semantic collection failure: {result.coverage}: {result.reason}"
                            )
            else:
                result = await handler(
                    self,
                    url,
                    work_id,
                    limit,
                    page=page,
                    include_stats=include_stats,
                    include_comments=include_comments,
                )

            result.protocol_attempts = attempt
            should_retry = self._should_retry_protocol(
                result,
                include_stats=include_stats,
                include_comments=include_comments,
            )
            if not should_retry or attempt == self.max_protocol_attempts:
                return result

            delay = self._retry_delay(platform, attempt)
            logger.warning(
                "protocol_retry platform=%s work_id=%s attempt=%s/%s delay=%.2f coverage=%s reason=%s",
                platform,
                work_id,
                attempt,
                self.max_protocol_attempts,
                delay,
                result.coverage,
                result.reason,
            )
            if delay:
                await asyncio.sleep(delay)

        assert result is not None
        return result

    def _retry_delay(self, platform: EngagementPlatform, attempt: int) -> float:
        empirical_floor = 4.0 if platform == "xiaohongshu" else 0.0
        base = max(self.protocol_retry_base_seconds, empirical_floor)
        return base * (2 ** (attempt - 1))

    @staticmethod
    def _should_retry_protocol(
        result: EngagementResult,
        *,
        include_stats: bool,
        include_comments: bool,
    ) -> bool:
        if result.coverage in {"blocked", "failed"}:
            return True
        if result.coverage in {"unsupported", "complete"}:
            return False
        if bool(getattr(result, "retryable_partial", False)):
            return True
        if include_stats and not any(
            value is not None for value in result.stats.model_dump().values()
        ):
            return True
        if include_comments and not result.comments and result.stats.comments != 0:
            return True
        return False

    @staticmethod
    def _should_use_browser_fallback(
        result: EngagementResult,
        *,
        include_stats: bool,
        include_comments: bool,
    ) -> bool:
        if result.coverage in {"unsupported", "blocked", "failed"}:
            return True
        if include_stats and not any(
            value is not None for value in result.stats.model_dump().values()
        ):
            return True
        if (
            include_comments
            and not result.comments
            and result.coverage == "partial"
            and result.stats.comments != 0
        ):
            return True
        return False

    async def _run_browser_fallback(
        self,
        url: str,
        platform: EngagementPlatform,
        work_id: str,
        *,
        page: int,
        limit: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult | None:
        if self.browser_fallback is None:
            return None
        try:
            return await self.browser_fallback.fetch(
                url,
                platform,
                work_id,
                page=page,
                limit=limit,
                include_stats=include_stats,
                include_comments=include_comments,
            )
        except Exception as exc:
            return result_error(
                platform,
                url,
                work_id,
                "failed",
                f"浏览器兜底调用失败: {type(exc).__name__}: {exc}",
            )

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._get_response(url, params=params, headers=headers)
        try:
            payload = response.json()
        except Exception as exc:
            raise PlatformCrawlerError("engagement endpoint returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformCrawlerError("engagement endpoint returned invalid JSON")
        return payload

    async def _post_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._post_response(
            url,
            params=params,
            headers=headers,
            data=data,
            json_body=json_body,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise PlatformCrawlerError("engagement endpoint returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformCrawlerError("engagement endpoint returned invalid JSON")
        return payload

    async def _get_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        include_cookies: bool = False,
    ) -> Any:
        request_headers = dict(headers or {})
        if include_cookies and self.cookies:
            request_headers["Cookie"] = self.cookies
        try:
            response = await self.client.get(url, params=params, headers=request_headers)
        except Exception as exc:
            raise PlatformCrawlerError("engagement request failed") from exc
        status = int(getattr(response, "status_code", 0))
        if status in {401, 403, 406, 412, 418, 429, 432, 461, 471}:
            raise PlatformBlockedError(f"engagement endpoint blocked with HTTP {status}")
        if status < 200 or status >= 300:
            raise PlatformCrawlerError(f"engagement endpoint returned HTTP {status}")
        return response

    async def _post_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        post = getattr(self.client, "post", None)
        if post is None:
            raise PlatformCrawlerError("HTTP client does not support POST")
        try:
            response = await post(
                url,
                params=params,
                headers=dict(headers or {}),
                data=data,
                json=json_body,
            )
        except Exception as exc:
            raise PlatformCrawlerError("engagement request failed") from exc
        status = int(getattr(response, "status_code", 0))
        if status in {401, 403, 406, 412, 418, 429, 432, 461, 471}:
            raise PlatformBlockedError(f"engagement endpoint blocked with HTTP {status}")
        if status < 200 or status >= 300:
            raise PlatformCrawlerError(f"engagement endpoint returned HTTP {status}")
        return response

    def _platform_cookie(self, platform: EngagementPlatform) -> str:
        configured = self.platform_cookies.get(platform, "").strip()
        if configured:
            return configured
        if self.session_store is not None:
            stored = self.session_store.cookie_header(platform).strip()
            if stored:
                return stored
        return self.cookies.strip() if platform == "douyin" else ""


async def fetch_engagement(
    url: str,
    *,
    comment_limit: int = 20,
    client: AsyncHttpClient | None = None,
    cookies: str = "",
    proxy_provider: Any | None = None,
    proxy_mode: str = "direct",
) -> EngagementResult:
    """Convenience function for one URL; closes the internally-created client."""
    crawler = EngagementCrawler(
        client=client,
        cookies=cookies,
        proxy_provider=proxy_provider,
        proxy_mode=proxy_mode,
    )
    try:
        return await crawler.fetch(url, comment_limit=comment_limit)
    finally:
        await crawler.aclose()


async def fetch_interactions(
    url: str,
    media_name: str,
    *,
    client: AsyncHttpClient | None = None,
    cookies: str = "",
    proxy_provider: Any | None = None,
    proxy_mode: str = "direct",
) -> InteractionResult:
    crawler = EngagementCrawler(
        client=client,
        cookies=cookies,
        proxy_provider=proxy_provider,
        proxy_mode=proxy_mode,
    )
    try:
        return await crawler.fetch_interactions(url, media_name)
    finally:
        await crawler.aclose()


async def fetch_comments(
    url: str,
    media_name: str,
    page: int,
    *,
    client: AsyncHttpClient | None = None,
    cookies: str = "",
    proxy_provider: Any | None = None,
    proxy_mode: str = "direct",
) -> CommentPageResult:
    crawler = EngagementCrawler(
        client=client,
        cookies=cookies,
        proxy_provider=proxy_provider,
        proxy_mode=proxy_mode,
    )
    try:
        return await crawler.fetch_comments(url, media_name, page)
    finally:
        await crawler.aclose()
