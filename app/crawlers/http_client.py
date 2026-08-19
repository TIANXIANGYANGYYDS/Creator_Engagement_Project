"""Asynchronous HTTP client with optional managed proxy leases."""

from __future__ import annotations

import inspect
from typing import Any, Literal, Protocol, runtime_checkable

from curl_cffi import requests as curl_requests

from app.crawlers.proxy_provider import AsyncProxyProvider, ProxyUnavailableError


class PlatformCrawlerError(RuntimeError):
    """Base error for platform requests and invalid responses."""


class PlatformBlockedError(PlatformCrawlerError):
    """The platform rejected an otherwise valid public request."""


@runtime_checkable
class AsyncHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
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
    ) -> None:
        if proxy_mode not in {"direct", "prefer", "required"}:
            raise ValueError("proxy_mode must be direct, prefer or required")
        self._session = session or curl_requests.AsyncSession(
            impersonate="chrome124",
            timeout=timeout_seconds,
            allow_redirects=True,
            headers=headers,
        )
        self.proxy_provider = proxy_provider
        self.proxy_mode = proxy_mode

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return await self._request("GET", url, params=params, headers=headers)

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
    ) -> Any:
        attempts = 2 if self.proxy_provider is not None and self.proxy_mode != "direct" else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            proxies = await self._acquire_proxy()
            proxy_url = (proxies or {}).get("https") or (proxies or {}).get("http")
            try:
                request = getattr(self._session, "request", None)
                if request is not None:
                    response = await request(
                        method,
                        url,
                        params=params,
                        headers=headers,
                        data=data,
                        json=json,
                        proxy=proxy_url,
                    )
                elif method == "GET":
                    response = await self._session.get(
                        url,
                        params=params,
                        headers=headers,
                        proxy=proxy_url,
                    )
                else:
                    response = await self._session.post(
                        url,
                        params=params,
                        headers=headers,
                        data=data,
                        json=json,
                        proxy=proxy_url,
                    )
            except Exception as exc:
                last_error = exc
                await self._notify_proxy("on_failure_for", "on_failure", proxies, exc)
                if attempt + 1 < attempts:
                    continue
                raise
            status = int(getattr(response, "status_code", 0))
            if status in {403, 407, 412, 418, 429, 432, 471} or status >= 500:
                await self._notify_proxy(
                    "on_failure_for",
                    "on_failure",
                    proxies,
                    RuntimeError(f"proxy request returned HTTP {status}"),
                )
            else:
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
