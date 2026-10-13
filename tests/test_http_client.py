from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.crawlers.http_client import CurlAsyncHttpClient
from app.crawlers.proxy_provider import ProxyUnavailableError
from curl_cffi import CurlInfo, CurlOpt, requests as curl_requests
from curl_cffi.requests.exceptions import ProxyError


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


def test_get_can_discard_response_cookies() -> None:
    session = FakeSession()
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_mode="direct",
        session=session,
    )

    asyncio.run(client.get("https://example.com", discard_cookies=True))

    assert session.calls[0]["discard_cookies"] is True


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


def test_account_direct_scope_covers_get_post_and_browser_without_leaking_to_other_tasks():
    proxies = {"https": "http://127.0.0.1:8080"}
    provider = CountingProxyProvider(proxies)
    session = FakeSession()
    client = CurlAsyncHttpClient(timeout_seconds=10, headers={}, proxy_mode="required",
        proxy_provider=provider, session=session)
    async def account():
        async with client.account_direct_scope(), client.lease_scope():
            assert await client.active_proxy_mapping() is None
            await client.get('https://edith.xiaohongshu.com/test')
            await asyncio.sleep(0)
            await client.post('https://edith.xiaohongshu.com/test')
    async def other():
        await client.get('https://example.com/')
    async def run():
        await asyncio.gather(account(), other())
        await other()
    asyncio.run(run())
    xhs = [c for c in session.calls if 'xiaohongshu.com' in c['url']]
    others = [c for c in session.calls if 'example.com' in c['url']]
    assert all(c['proxy'] is None and c['proxies'] == {'all': ''} for c in xhs)
    assert all(c['proxy'] == proxies['https'] and 'proxies' not in c for c in others)
    assert provider.acquisitions == 2


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


def test_isolated_session_scope_uses_private_cookie_and_connection_state() -> None:
    proxies = {"https": "http://127.0.0.1:8080"}
    provider = CountingProxyProvider(proxies)
    shared_session = FakeSession()
    isolated_session = FakeSession()
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=provider,
        proxy_mode="required",
        session=shared_session,
        session_factory=lambda: isolated_session,
    )

    async def run() -> None:
        async with client.lease_scope():
            async with client.isolated_session_scope():
                await client.get("https://example.com/detail")
                await client.post("https://example.com/comments")

    asyncio.run(run())

    assert shared_session.calls == []
    assert len(isolated_session.calls) == 2
    assert isolated_session.closed is True
    assert provider.acquisitions == 1
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


def test_required_mode_rejects_malformed_proxy_mapping() -> None:
    client = CurlAsyncHttpClient(
        timeout_seconds=10,
        headers={},
        proxy_provider=FakeProxyProvider({"unexpected": "value"}),
        proxy_mode="required",
        session=FakeSession(),
    )

    with pytest.raises(ProxyUnavailableError, match="禁止本机直连请求"):
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


def test_transport_failure_log_retains_error_code_without_secrets(caplog) -> None:
    class DiagnosticTimeout(Exception):
        code = 28
    async def scenario():
        client = CurlAsyncHttpClient(timeout_seconds=20, headers={}, session=FakeSession(
            error=DiagnosticTimeout("https://user:proxy-secret@proxy.example/?token=query-secret")))
        try:
            with pytest.raises(DiagnosticTimeout):
                await client.get("https://www.douyin.com/detail?token=signature-secret", headers={"Cookie": "cookie-secret"})
        finally:
            await client.aclose()
    asyncio.run(scenario())
    assert "code=28" in caplog.text
    assert "endpoint=www.douyin.com/detail" in caplog.text
    assert "secret" not in caplog.text


def test_connection_deadline_is_separate_from_total_timeout(monkeypatch) -> None:
    from curl_cffi import CurlOpt
    from app.crawlers import http_client
    settings = []
    def session_factory(**kwargs):
        settings.append(kwargs)
        return FakeSession()
    monkeypatch.setattr(http_client.curl_requests, "AsyncSession", session_factory)
    async def scenario():
        client = CurlAsyncHttpClient(timeout_seconds=20, connect_timeout_seconds=5, headers={})
        async with client.isolated_session_scope():
            pass
        await client.aclose()
    asyncio.run(scenario())
    assert len(settings) == 2
    assert all(s["timeout"] == 20 for s in settings)
    assert all(s["curl_options"][CurlOpt.CONNECTTIMEOUT_MS] == 5000 for s in settings)


def proxy_rejection(status=403):
    response = FakeResponse()
    response.infos = {CurlInfo.HTTP_CONNECTCODE: status}
    return ProxyError("CONNECT failed", code=56, response=response)


class RoutedSession(FakeSession):
    def __init__(self, outcome=None):
        super().__init__()
        self.outcome = outcome or FakeResponse()
        self.curl_options = {}
        self.cookies = curl_requests.Cookies()
        self.headers = curl_requests.Headers()

    async def request(self, method, url, **options):
        self.calls.append({"method": method, "url": url, **options})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        self.cookies.set("response_cookie", "new", domain="edith.xiaohongshu.com")
        return self.outcome


@pytest.mark.parametrize("host", ["edith.xiaohongshu.com", "res.wx.qq.com", "webapi.rednote.com"])
def test_xhs_connect_recovery_keeps_proxy_tls_target_and_cookie_flow(monkeypatch, host):
    async def run():
        proxy = {"https": "http://127.0.0.1:8080"}
        provider = CountingProxyProvider(proxy)
        original = RoutedSession(proxy_rejection())
        original.cookies.set("session_cookie", "existing", domain="edith.xiaohongshu.com")
        original.headers["User-Agent"] = "test-agent"
        fallback = RoutedSession()
        client = CurlAsyncHttpClient(timeout_seconds=5, headers={}, proxy_provider=provider,
                                     proxy_mode="required", session=original,
                                     session_factory=lambda: fallback)
        async def resolve(*args, **kwargs):
            return [(2, 1, 6, "", ("1.1.1.1", 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        try:
            async with client.lease_scope():
                result = await client.post(f"https://{host}/api/test",
                                           headers={"X-S": "signature"}, data="body")
            assert result.status_code == 200
            assert provider.acquisitions == 1 and provider.successes == [proxy]
            assert provider.failures == []
            assert original.calls == fallback.calls
            assert fallback.calls[0]["proxy"] == proxy["https"]
            assert "verify" not in fallback.calls[0]
            assert fallback.headers["User-Agent"] == "test-agent"
            assert fallback.cookies.get("session_cookie") == "existing"
            assert original.cookies.get("response_cookie") == "new"
            assert fallback.closed
            assert CurlOpt.CONNECT_TO not in original.curl_options
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("url,status", [
    ("https://other.example/api", 403),
    ("http://edith.xiaohongshu.com/api", 403),
    ("https://edith.xiaohongshu.com:444/api", 403),
    ("https://edith.xiaohongshu.com/api", 407),
    ("https://edith.xiaohongshu.com/api", 0),
])
def test_connect_recovery_does_not_apply_to_other_failures(url, status):
    async def run():
        original = RoutedSession(proxy_rejection(status))
        def forbidden_factory():
            pytest.fail("unexpected resolved retry")
        client = CurlAsyncHttpClient(timeout_seconds=5, headers={}, session=original,
                                    proxy_provider=FakeProxyProvider({"https": "http://proxy:80"}),
                                    proxy_mode="required", session_factory=forbidden_factory)
        try:
            async with client.lease_scope():
                with pytest.raises(ProxyError):
                    await client.get(url)
        finally:
            await client.aclose()
    asyncio.run(run())


def test_origin_403_is_not_replayed():
    async def run():
        original = RoutedSession(FakeResponse(403))
        def forbidden_factory():
            pytest.fail("origin request was replayed")
        client = CurlAsyncHttpClient(timeout_seconds=5, headers={}, session=original,
                                    session_factory=forbidden_factory)
        try:
            assert (await client.post("https://edith.xiaohongshu.com/api", data="body")).status_code == 403
            assert len(original.calls) == 1
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("resolved", ["127.0.0.1", "10.1.2.3", None])
def test_connect_recovery_rejects_private_dns_and_dns_failure(monkeypatch, resolved):
    async def run():
        async def resolve(*args, **kwargs):
            if resolved is None:
                raise TimeoutError()
            return [(2, 1, 6, "", (resolved, 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        original = RoutedSession(proxy_rejection())
        def forbidden_factory():
            pytest.fail("unsafe or unresolved destination")
        client = CurlAsyncHttpClient(timeout_seconds=5, headers={}, session=original,
                                    proxy_provider=FakeProxyProvider({"https": "http://proxy:80"}),
                                    proxy_mode="required", session_factory=forbidden_factory)
        try:
            async with client.lease_scope():
                with pytest.raises(ProxyError):
                    await client.get("https://edith.xiaohongshu.com/api")
        finally:
            await client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_resolved_retry_closes_session_on_error_or_cancellation(monkeypatch, cancelled):
    async def run():
        async def resolve(*args, **kwargs):
            return [(2, 1, 6, "", ("1.1.1.1", 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        original = RoutedSession(proxy_rejection())
        failure = asyncio.CancelledError() if cancelled else ConnectionError("failed")
        fallback = RoutedSession(failure)
        client = CurlAsyncHttpClient(timeout_seconds=5, headers={}, session=original,
                                    session_factory=lambda: fallback,
                                    proxy_provider=FakeProxyProvider({"https": "http://proxy:80"}),
                                    proxy_mode="required")
        try:
            async with client.lease_scope():
                with pytest.raises(type(failure)):
                    await client.get("https://edith.xiaohongshu.com/api")
            assert fallback.closed
            assert CurlOpt.CONNECT_TO not in original.curl_options
        finally:
            await client.aclose()
    asyncio.run(run())


def test_resolved_retry_honors_discard_cookies(monkeypatch):
    async def run():
        async def resolve(*args, **kwargs):
            return [(2, 1, 6, "", ("1.1.1.1", 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        original, fallback = RoutedSession(proxy_rejection()), RoutedSession()
        client = CurlAsyncHttpClient(timeout_seconds=5, headers={}, session=original,
                                    session_factory=lambda: fallback,
                                    proxy_provider=FakeProxyProvider({"https": "http://proxy:80"}),
                                    proxy_mode="required")
        try:
            await client.get("https://edith.xiaohongshu.com/api", discard_cookies=True)
            assert fallback.calls[0]["discard_cookies"] is True
            assert original.cookies.get("response_cookie") is None
        finally:
            await client.aclose()
    asyncio.run(run())


def test_account_direct_rate_limit_covers_each_upstream_request_only(monkeypatch):
    clock = [100.0]
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr('app.crawlers.http_client.monotonic', lambda: clock[0])
    monkeypatch.setattr('app.crawlers.http_client.asyncio.sleep', sleep)
    client = CurlAsyncHttpClient(timeout_seconds=1, headers={}, session=FakeSession(),
                                account_direct_min_interval_seconds=5)
    async def run():
        async with client.account_direct_scope():
            await client.get('https://edith.xiaohongshu.com/api/sns/web/v1/user_posted')
            await client.get('https://edith.xiaohongshu.com/api/sns/web/v2/comment/page')
            await client.get('https://edith.xiaohongshu.com/api/sns/web/v2/comment/sub/page')
        await client.get('https://example.com/')
    asyncio.run(run())
    assert sleeps == [5, 5]
