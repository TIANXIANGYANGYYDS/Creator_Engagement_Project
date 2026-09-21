"""Asynchronous HTTP client with optional managed proxy leases."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import inspect
from ipaddress import ip_address
import logging
import socket
from time import monotonic
from urllib.parse import urlsplit
from typing import Any, AsyncIterator, Callable, Literal, Protocol, runtime_checkable

from curl_cffi import CurlInfo, CurlOpt, ffi, lib, requests as curl_requests
from curl_cffi.requests.exceptions import ProxyError

from app.crawlers.proxy_provider import AsyncProxyProvider, ProxyUnavailableError


logger = logging.getLogger(__name__)

# These hosts have been verified to reject domain CONNECT at the proxy while
# accepting their DNS-resolved address. TLS still authenticates the URL host.
RESOLVED_PROXY_HOSTS = frozenset({
    "edith.xiaohongshu.com", "res.wx.qq.com", "webapi.rednote.com",
})


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
        connect_timeout_seconds: float = 5,
        headers: dict[str, str],
        proxy_provider: AsyncProxyProvider | None = None,
        proxy_mode: Literal["direct", "prefer", "required"] | str = "direct",
        session: Any | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        if proxy_mode not in {"direct", "prefer", "required"}:
            raise ValueError("proxy_mode must be direct, prefer or required")
        def default_session_factory():
            return curl_requests.AsyncSession(
                impersonate="chrome124",
                timeout=timeout_seconds,
                curl_options={
                    CurlOpt.CONNECTTIMEOUT_MS: max(
                        1, round(min(connect_timeout_seconds, timeout_seconds) * 1000),
                    ),
                },
                curl_infos=[CurlInfo.HTTP_CONNECTCODE],
                allow_redirects=True,
                headers=headers,
            )

        self._session_factory = session_factory or (
            default_session_factory if session is None else None
        )
        self._session = session or default_session_factory()
        self.proxy_provider = proxy_provider
        self.proxy_mode = proxy_mode
        self._dns_timeout = min(3.0, connect_timeout_seconds, timeout_seconds)
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

    def active_lease_remaining_seconds(self) -> float | None:
        state = self._lease_state.get()
        remaining = getattr(self.proxy_provider, "remaining_seconds", None)
        if state is None or not state.proxies or remaining is None:
            return None
        return remaining(state.proxies)

    async def active_proxy_mapping(self) -> dict[str, str] | None:
        """Acquire the current scoped lease for a browser sharing this operation."""
        state = self._lease_state.get()
        if state is None:
            raise RuntimeError("active_proxy_mapping requires lease_scope")
        if not state.acquired:
            state.proxies = await self._acquire_proxy()
            state.acquired = True
        return state.proxies

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
            started = monotonic()
            target = urlsplit(url)
            endpoint = f"{target.hostname}{target.path}"
            try:
                session = self._isolated_session.get() or self._session
                request = getattr(session, "request", None)
                if request is not None:
                    request_options = dict(
                        params=params, headers=headers, data=data, json=json,
                        proxy=proxy_url, discard_cookies=discard_cookies,
                    )
                    try:
                        response = await request(method, url, **request_options)
                    except ProxyError as exc:
                        response = await self._retry_resolved_proxy(
                            exc, session, method, url, request_options,
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
                # Exception messages and URLs can contain proxy credentials or
                # signed query parameters; log only bounded diagnostic fields.
                code = getattr(exc, "code", None)
                logger.warning(
                    "transport_failure method=%s endpoint=%s error_type=%s code=%s elapsed_ms=%s",
                    method, endpoint, type(exc).__name__,
                    code if isinstance(code, int) else None,
                    round((monotonic() - started) * 1000),
                )
                last_error = exc
                if lease_state is not None:
                    lease_state.failed = True
                else:
                    await self._notify_proxy("on_failure_for", "on_failure", proxies, exc)
                if attempt + 1 < attempts:
                    continue
                raise
            status = int(getattr(response, "status_code", 0))
            if status >= 400:
                logger.warning(
                    "transport_rejected method=%s endpoint=%s status=%s elapsed_ms=%s",
                    method, endpoint, status, round((monotonic() - started) * 1000),
                )
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

    async def _retry_resolved_proxy(
        self, error: ProxyError, session: Any, method: str, url: str,
        options: dict[str, Any],
    ) -> Any:
        """Retry a verified platform CONNECT rejection through the same proxy.

        Only a proxy's CONNECT 403 qualifies: the origin has not received the
        request. CONNECT_TO preserves the URL, Host, TLS SNI and certificate
        verification. Never apply this to an origin 403 or arbitrary hosts.
        """
        target = urlsplit(url)
        proxy = urlsplit(options.get("proxy") or "")
        infos = getattr(getattr(error, "response", None), "infos", {})
        if (target.hostname not in RESOLVED_PROXY_HOSTS or target.scheme != "https"
                or target.port not in (None, 443) or proxy.scheme not in ("http", "https")
                or infos.get(CurlInfo.HTTP_CONNECTCODE) != 403
                or self._session_factory is None):
            raise error
        try:
            addresses = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    target.hostname, 443, family=socket.AF_INET, type=socket.SOCK_STREAM,
                ), self._dns_timeout,
            )
        except (OSError, TimeoutError):
            raise error
        address = next((row[4][0] for row in addresses if ip_address(row[4][0]).is_global), None)
        if address is None:
            raise error
        rule = f"{target.hostname}:443:{address}:443".encode()
        route = lib.curl_slist_append(ffi.NULL, rule)
        if route == ffi.NULL:
            raise MemoryError("Cannot allocate proxy connection route")
        fallback = None
        try:
            # A separate session prevents concurrent requests from seeing this
            # connection rule. Keep cookies within the current isolated flow.
            fallback = self._session_factory()
            fallback.curl_options[CurlOpt.CONNECT_TO] = route
            fallback.cookies.update(session.cookies)
            fallback.headers.update(session.headers)
            logger.info("proxy_connect_resolved_retry host=%s connect_status=403", target.hostname)
            response = await fallback.request(method, url, **options)
            if not options.get("discard_cookies"):
                session.cookies.update(fallback.cookies)
            return response
        finally:
            try:
                if fallback is not None:
                    await fallback.close()
            finally:
                lib.curl_slist_free_all(route)

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
