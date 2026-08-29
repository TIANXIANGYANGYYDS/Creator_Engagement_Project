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
        force_direct: bool = False,
    ) -> dict[str, Any]: ...

    async def _post_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        force_direct: bool = False,
    ) -> Any: ...

    async def _get_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        include_cookies: bool = False,
        force_direct: bool = False,
        discard_cookies: bool = False,
    ) -> Any: ...

    def _platform_cookie(self, platform: EngagementPlatform) -> str: ...

    async def _ensure_weibo_visitor_session(
        self,
        target_url: str,
        *,
        entry: str,
    ) -> None: ...

    def _invalidate_weibo_visitor_session(self) -> None: ...

    async def _wechat_mp_access_token(self) -> str: ...

    def _invalidate_wechat_mp_access_token(self) -> None: ...

    async def _wechat_session_bridge_request(
        self,
        operation: str,
        *,
        url: str,
        metadata: dict[str, str],
        page: int,
        limit: int,
    ) -> dict[str, Any] | None: ...

    async def _wechat_channels_bridge_comments(
        self,
        *,
        url: str,
        page: int,
        limit: int,
    ) -> dict[str, Any] | None: ...

    async def _wechat_channels_bridge_interactions(
        self,
        *,
        url: str,
    ) -> dict[str, Any] | None: ...

class PlatformFetchHandler(Protocol):
    def __call__(
        self,
        crawler: PlatformCrawlerContext,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        comment_cursor: str | None,
        include_stats: bool,
        include_comments: bool,
    ) -> Awaitable[EngagementResult]: ...
