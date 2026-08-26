from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace

from app.crawlers import proxy_provider as proxy_module
from app.crawlers.proxy_provider import (
    AsyncDailiProxyPool,
    AsyncDailiProxyProvider,
    DailiProxyProvider,
    ProxyEndpoint,
)


def test_provider_builds_api_url_from_explicit_configuration() -> None:
    provider = DailiProxyProvider(
        minutes=3,
        count=4,
        api_url="https://proxy.example/getip?token=private&qty=1",
    )

    assert "token=private" in provider._build_api_url()
    assert "qty=4" in provider._build_api_url()


def test_async_provider_reuses_and_replaces_failed_lease(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(proxy_51_api_url="https://proxy.example/getip"),
    )
    provider = AsyncDailiProxyProvider(minutes=3)
    calls = 0

    async def fake_fetch() -> ProxyEndpoint:
        nonlocal calls
        calls += 1
        return ProxyEndpoint("127.0.0.1", 8000 + calls)

    monkeypatch.setattr(provider, "_fetch_proxy_endpoint_async", fake_fetch)

    async def run() -> None:
        try:
            first = await provider.get_requests_proxies()
            assert await provider.get_requests_proxies() == first
            assert calls == 1
            provider.on_failure_for(first, ConnectionError("failed"))
            second = await provider.get_requests_proxies()
            assert second != first
            assert calls == 2
        finally:
            await provider.close()

    asyncio.run(run())


def test_async_pool_limits_concurrency_and_refills_failed_slot(monkeypatch) -> None:
    monkeypatch.setattr(
        proxy_module,
        "Settings",
        lambda: SimpleNamespace(proxy_51_api_url="https://proxy.example/getip"),
    )
    provider = AsyncDailiProxyPool(minutes=3, pool_size=2, max_concurrency_per_proxy=2)
    batches: list[int] = []

    async def fake_fetch(count: int) -> list[ProxyEndpoint]:
        batches.append(count)
        base = 8000 if len(batches) == 1 else 9000
        return [ProxyEndpoint("127.0.0.1", base + index) for index in range(count)]

    monkeypatch.setattr(provider, "_fetch_proxy_endpoints_async", fake_fetch)

    async def run() -> None:
        try:
            async with provider.usage_scope() as usage:
                leases = await asyncio.gather(
                    *(provider.get_requests_proxies() for _ in range(4))
                )
            assert usage.added_endpoint_count == 2
            assert Counter(lease["https"] for lease in leases) == Counter({
                "http://127.0.0.1:8000": 2,
                "http://127.0.0.1:8001": 2,
            })
            assert batches == [2]

            await provider.on_failure_for(leases[0], ConnectionError("failed"))
            replacement = await provider.get_requests_proxies()
            assert replacement["https"].endswith(":9000")
            assert batches == [2, 1]
        finally:
            await provider.close()

    asyncio.run(run())
