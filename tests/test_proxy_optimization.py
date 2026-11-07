"""Offline regression checks for opt-in proxy admission and identity safety."""
from __future__ import annotations

import asyncio
from time import monotonic
from types import SimpleNamespace

import pytest

from app.crawlers import proxy_provider as pool_module
from app.crawlers.douyin_visitor import DouyinVisitor, DouyinVisitorSessions
from app.crawlers.browser_fallback import BrowserFallback
from app.crawlers.http_client import CurlAsyncHttpClient, PlatformCrawlerError
from app.crawlers.proxy_policy import ProxyFailure, ProxyRequirements
from app.crawlers.proxy_provider import AsyncDailiProxyPool, ProxyEndpoint


def make_pool(monkeypatch, **options):
    pool = AsyncDailiProxyPool(
        minutes=3, api_url="https://proxy.invalid/", lease_optimization_enabled=True,
        **options,
    )
    batches = []

    async def fetch(count):
        batches.append(count)
        return [ProxyEndpoint("127.0.0.1", 8000 + len(batches) * 100 + i) for i in range(count)]

    monkeypatch.setattr(pool, "_fetch_proxy_endpoints_async", fetch)
    return pool, batches


def test_long_job_replaces_only_one_idle_near_expiry_ip(monkeypatch):
    async def run():
        pool, batches = make_pool(monkeypatch, pool_size=2)
        try:
            old = await pool.get_requests_proxies()
            await pool.on_success_for(old)
            for slot in pool._slots:
                slot.expire_at = monotonic() + 30
            async with pool.requirements_scope(ProxyRequirements("fast", 25)):
                short = await pool.get_requests_proxies()
                assert pool.remaining_seconds(short) < 31
                await pool.on_success_for(short)
            async with pool.requirements_scope(ProxyRequirements("douyin", 70, True)):
                fresh = await pool.get_requests_proxies()
                assert fresh != old
                assert pool.remaining_seconds(fresh) > 70
                await pool.on_success_for(fresh)
            assert batches == [2, 1]
            assert pool.stats.ineligible_replacements == 1
            assert len(pool._slots) == 2
        finally:
            await pool.close()
    asyncio.run(run())


def test_exclusive_browser_waits_for_shared_leases_and_blocks_new_sharers(monkeypatch):
    async def run():
        pool, batches = make_pool(monkeypatch, pool_size=1, max_concurrency_per_proxy=20)
        browser_ready = asyncio.Event()
        browser_release = asyncio.Event()

        async def browser():
            async with pool.requirements_scope(ProxyRequirements("douyin", 70, True)):
                lease = await pool.get_requests_proxies()
                browser_ready.set()
                await browser_release.wait()
                await pool.on_success_for(lease)

        leases = await asyncio.gather(*(pool.get_requests_proxies() for _ in range(20)))
        task = asyncio.create_task(browser())
        try:
            await asyncio.sleep(0)
            assert not browser_ready.is_set()
            assert pool.stats.max_per_ip_in_flight == 20
            for lease in leases:
                await pool.on_success_for(lease)
            await asyncio.wait_for(browser_ready.wait(), 1)
            shared = asyncio.create_task(pool.get_requests_proxies())
            await asyncio.sleep(0)
            assert not shared.done()
            browser_release.set()
            await task
            lease = await asyncio.wait_for(shared, 1)
            await pool.on_success_for(lease)
            assert batches == [1]
            assert pool._slots[0].in_flight == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await pool.close()
    asyncio.run(run())


def test_platform_failure_keeps_ip_available_to_other_platforms(monkeypatch):
    async def run():
        pool, batches = make_pool(monkeypatch, pool_size=2)
        try:
            async with pool.requirements_scope(ProxyRequirements("weibo", 40)):
                first = await pool.get_requests_proxies()
                await pool.on_failure_for(first, ProxyFailure("429", kind="platform", platform="weibo"))
                second = await pool.get_requests_proxies()
                assert second != first
                await pool.on_success_for(second)
            async with pool.requirements_scope(ProxyRequirements("bilibili", 60)):
                leases = await asyncio.gather(pool.get_requests_proxies(), pool.get_requests_proxies())
                assert first in leases
                for lease in leases:
                    await pool.on_success_for(lease)
            assert batches == [2]
            assert pool.stats.discarded_endpoint_count == 0
        finally:
            await pool.close()
    asyncio.run(run())


def test_neutral_business_failure_does_not_poison_ip(monkeypatch):
    async def run():
        pool, batches = make_pool(monkeypatch, pool_size=1)
        try:
            first = await pool.get_requests_proxies()
            await pool.on_failure_for(first, ProxyFailure("private content", kind="task"))
            assert await pool.get_requests_proxies() == first
            await pool.on_success_for(first)
            assert batches == [1]
            assert pool.stats.task_failures == 1
        finally:
            await pool.close()
    asyncio.run(run())


def test_repeated_transport_failures_drain_only_after_last_borrower(monkeypatch):
    async def run():
        pool, _ = make_pool(monkeypatch, pool_size=1, max_concurrency_per_proxy=4)
        try:
            leases = await asyncio.gather(*(pool.get_requests_proxies() for _ in range(4)))
            slot = pool._slots[0]
            for lease in leases[:3]:
                await pool.on_failure_for(lease, ProxyFailure("connect timeout", kind="transport"))
            assert slot.draining and slot.in_flight == 1
            replacement = await pool.get_requests_proxies()
            assert replacement != leases[0]
            await pool.on_success_for(leases[3])
            assert pool._find_slot(replacement).in_flight == 1
            await pool.on_success_for(replacement)
            assert slot not in pool._slots
        finally:
            await pool.close()
    asyncio.run(run())


def test_supplier_delay_is_not_added_to_lifetime(monkeypatch):
    async def run():
        now = [100.0]
        monkeypatch.setattr(pool_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
        pool, _ = make_pool(monkeypatch, pool_size=1)

        async def fetch(count):
            now[0] += 50
            return [ProxyEndpoint("127.0.0.1", 8000)]

        monkeypatch.setattr(pool, "_fetch_proxy_endpoints_async", fetch)
        try:
            lease = await pool.get_requests_proxies()
            assert 115 <= pool.remaining_seconds(lease) <= 120
            await pool.on_success_for(lease)
        finally:
            await pool.close()
    asyncio.run(run())


def test_supplier_repeating_retired_ip_returns_bounded_failure(monkeypatch):
    async def run():
        pool, _ = make_pool(monkeypatch, pool_size=1)
        calls = []

        async def fetch(count):
            calls.append(count)
            return [ProxyEndpoint("127.0.0.1", 8000)]

        monkeypatch.setattr(pool, "_fetch_proxy_endpoints_async", fetch)
        try:
            lease = await pool.get_requests_proxies()
            await pool.on_success_for(lease)
            pool._slots[0].expire_at = monotonic() + 10
            async with pool.requirements_scope(ProxyRequirements("douyin", 70)):
                assert await asyncio.wait_for(pool.get_requests_proxies(), 1) is None
            assert calls == [1, 1]
            assert not pool._fetching
        finally:
            await pool.close()
    asyncio.run(run())


class Session:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(kwargs["proxy"])
        return SimpleNamespace(status_code=200)

    async def close(self):
        pass


def test_unused_lease_can_rotate_but_started_identity_cannot(monkeypatch):
    async def run():
        pool, _ = make_pool(monkeypatch, pool_size=1)
        session = Session()
        client = CurlAsyncHttpClient(timeout_seconds=20, headers={}, proxy_provider=pool,
                                    proxy_mode="required", session=session)
        try:
            async with client.collection_lease_scope("douyin"):
                first = await client.active_proxy_mapping()
                pool._slots[0].expire_at = monotonic() + 30
                await client.ensure_proxy_lifetime(70, exclusive=True)
                fresh = await client.active_proxy_mapping()
                assert fresh != first
                await client.get("https://target.invalid/")
                pool._find_slot(fresh).expire_at = monotonic() + 10
                with pytest.raises(PlatformCrawlerError, match="会话边界"):
                    await client.get("https://target.invalid/next")
            assert session.calls == [fresh["https"]]
            assert all(slot.in_flight == 0 for slot in pool._slots)
            assert pool.stats.task_failures == 1
        finally:
            await client.aclose()
    asyncio.run(run())


def test_cancelled_operation_releases_lease_without_discarding_ip(monkeypatch):
    async def run():
        pool, _ = make_pool(monkeypatch, pool_size=1)
        client = CurlAsyncHttpClient(timeout_seconds=20, headers={}, proxy_provider=pool,
                                    proxy_mode="required", session=Session())
        entered = asyncio.Event()

        async def work():
            async with client.collection_lease_scope("weibo"):
                await client.active_proxy_mapping()
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        try:
            await entered.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert pool._slots[0].in_flight == 0
            assert pool.stats.task_failures == 1
            assert pool.stats.discarded_endpoint_count == 0
        finally:
            await client.aclose()
    asyncio.run(run())


def test_douyin_rechecks_after_bootstrap_queue_wait(monkeypatch):
    async def run():
        pool, _ = make_pool(monkeypatch, pool_size=1)
        client = CurlAsyncHttpClient(timeout_seconds=20, headers={}, proxy_provider=pool,
                                    proxy_mode="required", session=Session())
        sessions = DouyinVisitorSessions(client)
        seen = []

        async def bootstrap(referer):
            seen.append(await client.active_proxy_mapping())
            assert client.active_lease_remaining_seconds() >= 70
            return DouyinVisitor({}, {}, monotonic() + 100)

        monkeypatch.setattr(sessions, "_bootstrap", bootstrap)

        async def work():
            async with client.collection_lease_scope("douyin"):
                return await sessions.get("https://www.douyin.com/")

        await sessions._lock.acquire()
        task = asyncio.create_task(work())
        try:
            # No supplier or upstream network: wait only for the borrowed slot.
            for _ in range(20):
                await asyncio.sleep(0)
                if pool._slots and pool._slots[0].in_flight:
                    break
            old = pool._slots[0].proxies
            pool._slots[0].expire_at = monotonic() + 15
            sessions._lock.release()
            visitor = await asyncio.wait_for(task, 1)
            assert seen[0] != old
            assert sessions._sessions[tuple(sorted(seen[0].items()))] is visitor
            assert tuple(sorted(old.items())) not in sessions._sessions
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
    asyncio.run(run())


def test_supplier_concurrency_limit_is_enforced():
    with pytest.raises(ValueError, match="20"):
        AsyncDailiProxyPool(minutes=3, max_concurrency_per_proxy=21,
                           lease_optimization_enabled=True, api_url="https://proxy.invalid/")


def test_unscoped_http_rejection_cools_down_target_host(monkeypatch):
    async def run():
        pool, _ = make_pool(monkeypatch, pool_size=2)
        session = Session()

        async def rejected(url, **kwargs):
            session.calls.append(kwargs["proxy"])
            return SimpleNamespace(status_code=429)

        session.get = rejected
        client = CurlAsyncHttpClient(timeout_seconds=20, headers={}, proxy_provider=pool,
                                    proxy_mode="required", session=session)
        try:
            await client.get("https://target.invalid/")
            await client.get("https://target.invalid/")
            assert len(set(session.calls)) == 2
            assert pool.stats.platform_cooldowns == 2
            assert all(slot.in_flight == 0 for slot in pool._slots)
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("optimized", [False, True])
def test_aged_ip_douyin_bootstrap_baseline_vs_candidate(monkeypatch, optimized):
    async def run():
        now = [100.0]
        monkeypatch.setattr(pool_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
        pool, batches = make_pool(monkeypatch, pool_size=1)
        pool.lease_optimization_enabled = optimized
        client = CurlAsyncHttpClient(timeout_seconds=20, headers={}, proxy_provider=pool,
                                    proxy_mode="required", session=Session())
        sessions = DouyinVisitorSessions(client)
        old = await pool.get_requests_proxies()
        await pool.on_success_for(old)
        pool._slots[0].expire_at = now[0] + 15

        async def bootstrap(referer):
            # Same 20-second initialization against an IP with 15 seconds left.
            now[0] += 20
            if client.active_lease_remaining_seconds() <= 10:
                raise PlatformCrawlerError("抖音访客代理即将过期")
            return DouyinVisitor({}, {}, monotonic() + 100)

        monkeypatch.setattr(sessions, "_bootstrap", bootstrap)
        try:
            async with client.collection_lease_scope("douyin"):
                if optimized:
                    assert await sessions.get("url")
                    assert await client.active_proxy_mapping() != old
                    assert len(batches) == 2
                else:
                    with pytest.raises(PlatformCrawlerError, match="即将过期"):
                        await sessions.get("url")
                    assert len(batches) == 1
        finally:
            await client.aclose()
    asyncio.run(run())


def test_douyin_does_not_reuse_visitor_with_insufficient_cookie_life(monkeypatch):
    async def run():
        pool, _ = make_pool(monkeypatch, pool_size=1)
        client = CurlAsyncHttpClient(timeout_seconds=20, headers={}, proxy_provider=pool,
                                    proxy_mode="required", session=Session())
        sessions = DouyinVisitorSessions(client)
        fresh = DouyinVisitor({}, {}, monotonic() + 100)

        async def bootstrap(referer):
            return fresh

        monkeypatch.setattr(sessions, "_bootstrap", bootstrap)
        try:
            async with client.collection_lease_scope("douyin"):
                mapping = await client.active_proxy_mapping()
                key = tuple(sorted(mapping.items()))
                sessions._sessions[key] = DouyinVisitor({}, {}, monotonic() + 5)
                assert await sessions.get("url") is fresh
        finally:
            await client.aclose()
    asyncio.run(run())


def test_generic_browser_and_author_admission_use_exclusive_fresh_ip(monkeypatch):
    async def run():
        pool, _ = make_pool(monkeypatch, pool_size=1)
        client = CurlAsyncHttpClient(timeout_seconds=20, headers={}, proxy_provider=pool,
                                    proxy_mode="required", session=Session())
        fallback = BrowserFallback(proxy_provider=pool, proxy_required=True)
        seen = []

        async def fetch_locked(*args, **kwargs):
            lease = await pool.get_requests_proxies()
            seen.append(pool._requirements.get())
            assert pool.remaining_seconds(lease) >= 90
            assert pool._find_slot(lease).exclusive
            await pool.on_success_for(lease)
            return "browser result"

        monkeypatch.setattr(fallback, "_fetch_locked", fetch_locked)
        try:
            lease = await pool.get_requests_proxies()
            await pool.on_success_for(lease)
            pool._slots[0].expire_at = monotonic() + 40
            assert await fallback.fetch("url", "weibo", "id", page=1, limit=20,
                                        include_stats=True, include_comments=False) == "browser result"
            assert seen == [ProxyRequirements("weibo", 90, True)]
            async with client.collection_lease_scope("weibo", author=True):
                lease = await client.active_proxy_mapping()
                assert pool.remaining_seconds(lease) >= 60
                assert pool._find_slot(lease).exclusive
            assert pool._requirements.get() == ProxyRequirements()
            assert pool._slots[0].in_flight == 0
        finally:
            await client.aclose()
    asyncio.run(run())
