"""Minimal contract shared by platform collectors and the central router."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol

from app.models.engagement import EngagementPlatform, EngagementResult


class PlatformCrawlerContext(Protocol):
    cookies: str

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def _post_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def _post_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any: ...

    async def _get_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        include_cookies: bool = False,
        force_direct: bool = False,
    ) -> Any: ...

    def _platform_cookie(self, platform: EngagementPlatform) -> str: ...


class PlatformFetchHandler(Protocol):
    def __call__(
        self,
        crawler: PlatformCrawlerContext,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> Awaitable[EngagementResult]: ...
