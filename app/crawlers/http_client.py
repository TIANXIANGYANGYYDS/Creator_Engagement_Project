"""Asynchronous HTTP client with optional managed proxy leases."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import inspect
from typing import Any, AsyncIterator, Callable, Literal, Protocol, runtime_checkable

from curl_cffi import requests as curl_requests

from app.crawlers.proxy_provider import AsyncProxyProvider, ProxyUnavailableError


class PlatformCrawlerError(RuntimeError):
    """Base error for platform requests and invalid responses."""


class PlatformBlockedError(PlatformCrawlerError):
    """The platform rejected an otherwise valid public request."""


@dataclass
class _ProxyLeaseState:
    proxies: dict[str, str] | None = None
    acquired: bool = False
    failed: bool = False
    failure_reason: str = ""


@runtime_checkable
class AsyncHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        discard_cookies: bool = False,
    ) -> Any:
        ...

    async def post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        ...


class CurlAsyncHttpClient:
    """Reusable protocol client with a Chrome-compatible TLS fingerprint."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        headers: dict[str, str],
        proxy_provider: AsyncProxyProvider | None = None,
        proxy_mode: Literal["direct", "prefer", "required"] | str = "direct",
        session: Any | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        if proxy_mode not in {"direct", "prefer", "required"}:
            raise ValueError("proxy_mode must be direct, prefer or required")
        default_session_factory = lambda: curl_requests.AsyncSession(
            impersonate="chrome124",
            timeout=timeout_seconds,
            allow_redirects=True,
            headers=headers,
        )
        self._session_factory = session_factory or (
            default_session_factory if session is None else None
        )
        self._session = session or default_session_factory()
        self.proxy_provider = proxy_provider
        self.proxy_mode = proxy_mode
        self._lease_state: ContextVar[_ProxyLeaseState | None] = ContextVar(
            "creator_engagement_proxy_lease",
            default=None,
        )
        self._force_direct: ContextVar[bool] = ContextVar(
            "creator_engagement_force_direct",
            default=False,
        )
        self._isolated_session: ContextVar[Any | None] = ContextVar(
            "creator_engagement_isolated_http_session",
            default=None,
        )

    @asynccontextmanager
    async def direct_scope(self) -> AsyncIterator[None]:
        """Temporarily bypass a configured preferred proxy for one request path."""

        token = self._force_direct.set(True)
        try:
            yield
        finally:
            self._force_direct.reset(token)

    @asynccontextmanager
    async def isolated_session_scope(self) -> AsyncIterator[None]:
        """Use a private cookie jar and connection pool for one protocol flow."""

        if self._isolated_session.get() is not None or self._session_factory is None:
            yield
            return
        session = self._session_factory()
        token = self._isolated_session.set(session)
        try:
            yield
        finally:
            self._isolated_session.reset(token)
            await session.close()

    @asynccontextmanager
    async def lease_scope(self) -> AsyncIterator[None]:
        """Keep one lazily-acquired proxy lease for one collection operation."""

        state = _ProxyLeaseState()
        token = self._lease_state.set(state)
        try:
            yield
        finally:
            self._lease_state.reset(token)
            if state.acquired and state.proxies is not None:
                callback = "on_failure_for" if state.failed else "on_success_for"
                error = (
                    RuntimeError(state.failure_reason or "collection proxy lease failed")
                    if state.failed
                    else None
                )
                await self._notify_proxy(
                    callback,
                    "on_failure" if state.failed else "on_success",
                    state.proxies,
                    error,
                )

    def invalidate_active_lease(self, reason: str) -> None:
        """Discard the current proxy after a semantic block or unusable payload."""

        state = self._lease_state.get()
        if state is None:
            return
        state.failed = True
        state.failure_reason = reason

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        discard_cookies: bool = False,
    ) -> Any:
        return await self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            discard_cookies=discard_cookies,
        )

    async def post(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request(
            "POST",
            url,
            params=params,
            headers=headers,
            data=data,
            json=json,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        discard_cookies: bool = False,
    ) -> Any:
        lease_state = self._lease_state.get()
        force_direct = self._force_direct.get() and self.proxy_mode != "required"
        attempts = (
            1
            if lease_state is not None or force_direct
            else (2 if self.proxy_provider is not None and self.proxy_mode != "direct" else 1)
        )
        last_error: Exception | None = None
        for attempt in range(attempts):
            if force_direct:
                proxies = None
            elif lease_state is not None:
                if not lease_state.acquired:
                    lease_state.proxies = await self._acquire_proxy()
                    lease_state.acquired = True
                proxies = lease_state.proxies
            else:
                proxies = await self._acquire_proxy()
            proxy_url = (proxies or {}).get("https") or (proxies or {}).get("http")
            if self.proxy_mode == "required" and not proxy_url:
                error = ProxyUnavailableError(
                    "代理映射缺少 http/https 地址，禁止本机直连请求"
                )
                if lease_state is not None:
                    lease_state.failed = True
                    lease_state.failure_reason = str(error)
                else:
                    await self._notify_proxy(
                        "on_failure_for",
                        "on_failure",
                        proxies,
                        error,
                    )
                raise error
            try:
                session = self._isolated_session.get() or self._session
                request = getattr(session, "request", None)
                if request is not None:
                    response = await request(
                        method,
                        url,
                        params=params,
                        headers=headers,
                        data=data,
                        json=json,
                        proxy=proxy_url,
                        discard_cookies=discard_cookies,
                    )
                elif method == "GET":
                    response = await session.get(
                        url,
                        params=params,
                        headers=headers,
                        proxy=proxy_url,
                        discard_cookies=discard_cookies,
                    )
                else:
                    response = await session.post(
                        url,
                        params=params,
                        headers=headers,
                        data=data,
                        json=json,
                        proxy=proxy_url,
                    )
            except Exception as exc:
                last_error = exc
                if lease_state is not None:
                    lease_state.failed = True
                else:
                    await self._notify_proxy("on_failure_for", "on_failure", proxies, exc)
                if attempt + 1 < attempts:
                    continue
                raise
            status = int(getattr(response, "status_code", 0))
            if status in {403, 407, 412, 418, 429, 432, 471} or status >= 500:
                if lease_state is not None:
                    lease_state.failed = True
                else:
                    await self._notify_proxy(
                        "on_failure_for",
                        "on_failure",
                        proxies,
                        RuntimeError(f"proxy request returned HTTP {status}"),
                    )
            elif lease_state is None:
                await self._notify_proxy("on_success_for", "on_success", proxies)
            return response
        assert last_error is not None
        raise last_error

    async def _acquire_proxy(self) -> dict[str, str] | None:
        if self.proxy_mode == "direct":
            return None
        if self.proxy_provider is None:
            if self.proxy_mode == "required":
                raise ProxyUnavailableError("代理模式为 required，但没有配置代理提供器")
            return None
        proxies = await self.proxy_provider.get_requests_proxies()
        if proxies is None and self.proxy_mode == "required":
            raise ProxyUnavailableError("未获取到代理，禁止本机直连请求")
        return proxies

    async def _notify_proxy(
        self,
        scoped_name: str,
        fallback_name: str,
        proxies: dict[str, str] | None,
        exc: Exception | None = None,
    ) -> None:
        if self.proxy_provider is None or proxies is None:
            return
        callback = getattr(self.proxy_provider, scoped_name, None)
        args: tuple[Any, ...] = (proxies, exc) if exc is not None else (proxies,)
        if callback is None:
            callback = getattr(self.proxy_provider, fallback_name, None)
            args = (exc,) if exc is not None else ()
        if callback is None:
            return
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    async def aclose(self) -> None:
        await self._session.close()
