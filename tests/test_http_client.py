from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.crawlers.http_client import CurlAsyncHttpClient
from app.crawlers.proxy_provider import ProxyUnavailableError


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class FakeSession:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or FakeResponse()
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self) -> None:
        self.closed = True


class FakeProxyProvider:
    def __init__(self, proxies: dict[str, str] | None) -> None:
        self.proxies = proxies
        self.successes: list[dict[str, str] | None] = []
        self.failures: list[tuple[dict[str, str] | None, Exception]] = []

    async def get_requests_proxies(self) -> dict[str, str] | None:
        return self.proxies

    async def on_success_for(self, proxies: dict[str, str] | None) -> None:
        self.successes.append(proxies)

    async def on_failure_for(
        self,
        proxies: dict[str, str] | None,
        exc: Exception,
    ) -> None:
        self.failures.append((proxies, exc))

    async def close(self) -> None:
        return None


class CountingProxyProvider(FakeProxyProvider):
    def __init__(self, proxies: dict[str, str]) -> None:
        super().__init__(proxies)
        self.acquisitions = 0

    async def get_requests_proxies(self) -> dict[str, str] | None:
        self.acquisitions += 1
        return self.proxies


class SequencedSession:
    def __init__(self, *outcomes: FakeResponse | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        return None


class RotatingProxyProvider(FakeProxyProvider):
    def __init__(self, *proxies: dict[str, str]) -> None:
        super().__init__(None)
        self.queue = list(proxies)

    async def get_requests_proxies(self) -> dict[str, str] | None:
        return self.queue.pop(0) if self.queue else None


def test_prefer_mode_uses_and_releases_proxy_lease() -> None:
    proxies = {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
    provider = FakeProxyProvider(proxies)
    session = FakeSession()
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="prefer",
        session=session,
    )

    asyncio.run(client.get("https://example.com"))

    assert session.calls[0]["proxy"] == proxies["https"]
    assert provider.successes == [proxies]
    assert provider.failures == []


def test_direct_scope_bypasses_preferred_proxy_inside_lease() -> None:
    proxies = {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
    provider = CountingProxyProvider(proxies)
    session = FakeSession()
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="prefer",
        session=session,
    )

    async def run() -> None:
        async with client.lease_scope():
            async with client.direct_scope():
                await client.get("https://example.com")

    asyncio.run(run())

    assert session.calls[0]["proxy"] is None
    assert provider.acquisitions == 0
    assert provider.successes == []
    assert provider.failures == []


def test_direct_scope_respects_required_proxy_mode() -> None:
    proxies = {"https": "http://127.0.0.1:8080"}
    provider = CountingProxyProvider(proxies)
    session = FakeSession()
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="required",
        session=session,
    )

    async def run() -> None:
        async with client.direct_scope():
            await client.get("https://example.com")

    asyncio.run(run())

    assert session.calls[0]["proxy"] == proxies["https"]
    assert provider.acquisitions == 1
    assert provider.successes == [proxies]


def test_post_uses_and_releases_proxy_lease() -> None:
    proxies = {"https": "http://127.0.0.1:8080"}
    provider = FakeProxyProvider(proxies)
    session = FakeSession()
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="prefer",
        session=session,
    )

    asyncio.run(client.post("https://example.com/graphql", json={"query": "detail"}))

    assert session.calls[0]["proxy"] == proxies["https"]
    assert session.calls[0]["json"] == {"query": "detail"}
    assert provider.successes == [proxies]


def test_collection_scope_reuses_one_proxy_for_multiple_requests() -> None:
    proxies = {"https": "http://127.0.0.1:8080"}
    provider = CountingProxyProvider(proxies)
    session = FakeSession()
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="prefer",
        session=session,
    )

    async def run() -> None:
        async with client.lease_scope():
            await client.get("https://example.com/detail")
            await client.post("https://example.com/comments")

    asyncio.run(run())

    assert provider.acquisitions == 1
    assert [call["proxy"] for call in session.calls] == [proxies["https"], proxies["https"]]
    assert provider.successes == [proxies]


def test_semantic_failure_discards_active_proxy_lease() -> None:
    proxies = {"https": "http://127.0.0.1:8080"}
    provider = CountingProxyProvider(proxies)
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="prefer",
        session=FakeSession(),
    )

    async def run() -> None:
        async with client.lease_scope():
            await client.get("https://example.com/data")
            client.invalidate_active_lease("HTTP 200 payload requested captcha")

    asyncio.run(run())

    assert provider.successes == []
    assert len(provider.failures) == 1
    assert provider.failures[0][0] == proxies
    assert "captcha" in str(provider.failures[0][1])


def test_blocked_response_discards_proxy_lease() -> None:
    proxies = {"https": "http://127.0.0.1:8080"}
    provider = FakeProxyProvider(proxies)
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="prefer",
        session=FakeSession(FakeResponse(403)),
    )

    asyncio.run(client.get("https://example.com"))

    assert len(provider.failures) == 1
    assert provider.successes == []


def test_required_mode_never_falls_back_to_direct() -> None:
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_mode="required",
        session=FakeSession(),
    )

    with pytest.raises(ProxyUnavailableError, match="没有配置代理提供器"):
        asyncio.run(client.get("https://example.com"))


def test_transport_error_discards_proxy_and_retries_once() -> None:
    first = {"https": "http://127.0.0.1:8080"}
    second = {"https": "http://127.0.0.2:8080"}
    provider = RotatingProxyProvider(first, second)
    session = SequencedSession(ConnectionError("connect failed"), FakeResponse(200))
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="prefer",
        session=session,
    )

    response = asyncio.run(client.get("https://example.com"))

    assert response.status_code == 200
    assert [call["proxy"] for call in session.calls] == [first["https"], second["https"]]
    assert provider.failures[0][0] == first
    assert provider.successes == [second]
