import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.crawlers.http_client import CurlAsyncHttpClient, PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platform_session import PlatformSessionStore
from app.crawlers import xiaohongshu_session as session
from app.crawlers.platforms import xiaohongshu as protocol


URL = "https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?note_id=abc&cursor=&image_formats=jpg,webp"


def make_owner(tmp_path, monkeypatch, responses):
    clock = SimpleNamespace(now=100.0, sleeps=[])
    async def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds
    monkeypatch.setattr(session.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(session.random, "uniform", lambda *args: 0)
    monkeypatch.setattr(session.asyncio, "sleep", sleep)
    monkeypatch.setattr(protocol, "signed_headers", lambda *args, **kwargs: {"X-S": "fresh"})
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=responses))
    context = SimpleNamespace(cookies=AsyncMock(return_value=[{"name": "a1", "value": "current"}]),
                              storage_state=AsyncMock(return_value={"cookies": [], "origins": []}))
    owner = session.XiaohongshuSessionOwner(context, page, PlatformSessionStore(tmp_path / "cookies"), tmp_path)
    owner.set_status("ready")
    return owner, clock


def response(code=0, status=200):
    return {"status": status, "url": URL, "text": json.dumps({"code": code, "success": code == 0, "data": {}})}


@pytest.mark.parametrize("method,url", [
    ("POST", "https://edith.xiaohongshu.com/api/sns/web/v1/note/like"),
    ("GET", "https://www.xiaohongshu.com.evil.test/explore/abc"),
    ("GET", "http://www.xiaohongshu.com/explore/abc"),
    ("GET", "https://user:pass@www.xiaohongshu.com/explore/abc"),
])
def test_owner_only_accepts_allowlisted_reads(method, url):
    with pytest.raises(ValueError):
        session.validate_read(method, url)


def test_expiry_halts_queued_reads_and_is_not_cleared_by_health(tmp_path, monkeypatch):
    owner, _ = make_owner(tmp_path, monkeypatch, [response(-100)])
    async def run():
        results = await asyncio.gather(owner.request("GET", URL), owner.request("GET", URL), return_exceptions=True)
        assert all(isinstance(result, PlatformBlockedError) for result in results)
    asyncio.run(run())
    assert owner.page.evaluate.await_count == 1
    assert json.loads((tmp_path / "status.json").read_text())["status"] == "reauthorization_required"


def test_network_errors_are_not_reported_as_logout(tmp_path, monkeypatch):
    owner, _ = make_owner(tmp_path, monkeypatch, [TimeoutError(), TimeoutError(), TimeoutError()])
    async def run():
        for _ in range(3):
            with pytest.raises(PlatformCrawlerError):
                await owner.request("GET", URL)
        with pytest.raises(PlatformBlockedError):
            await owner.request("GET", URL)
    asyncio.run(run())
    assert owner.status == "transport_stopped"
    assert owner.page.evaluate.await_count == 3


def test_global_pacing_and_fresh_browser_cookie_signing(tmp_path, monkeypatch):
    owner, clock = make_owner(tmp_path, monkeypatch, [response()] * 11)
    cookies = []
    def sign(uri, cookie, **kwargs):
        cookies.append(cookie)
        assert kwargs["params"]["cursor"] == ""
        return {"X-S": "fresh"}
    monkeypatch.setattr(protocol, "signed_headers", sign)
    async def run():
        for _ in range(11):
            await owner.request("GET", URL)
    asyncio.run(run())
    assert clock.sleeps == [15] * 9 + [105]
    assert cookies == ["a1=current"] * 11
    sent = owner.page.evaluate.await_args.args[1]
    assert sent["headers"] == {"X-S": "fresh"}
    assert "image_formats=jpg,webp" in sent["url"]


def test_cors_hidden_risk_response_still_stops_account(tmp_path, monkeypatch):
    owner, _ = make_owner(tmp_path, monkeypatch, [])
    owner.pending_url = URL
    asyncio.run(owner.observe_network(SimpleNamespace(url=URL, status=461)))
    assert owner.status == "risk_limited"


def test_saved_cookie_state_is_atomic_and_private(tmp_path):
    store = PlatformSessionStore(tmp_path)
    path = store.state_path("xiaohongshu")
    old = {"cookies": [{"name": "a1", "value": "old"}]}
    path.write_text(json.dumps(old))
    async def broken_state():
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        asyncio.run(store.save_context("xiaohongshu", SimpleNamespace(storage_state=broken_state)))
    assert json.loads(path.read_text()) == old
    new = {"cookies": [{"name": "a1", "value": "new"}]}
    asyncio.run(store.save_context("xiaohongshu", SimpleNamespace(storage_state=AsyncMock(return_value=new))))
    assert json.loads(path.read_text()) == new
    assert path.stat().st_mode & 0o777 == 0o600


def test_owner_unavailable_never_falls_back_to_curl_or_proxy(tmp_path):
    curl = SimpleNamespace(get=AsyncMock(), close=AsyncMock())
    client = CurlAsyncHttpClient(timeout_seconds=5, headers={}, session=curl,
                                proxy_mode="required", xiaohongshu_session_socket=str(tmp_path / "missing.sock"))
    async def run():
        with pytest.raises(PlatformBlockedError, match="禁止回退"):
            await client.get(URL, headers={"Cookie": "must-not-leave-caller"})
        await client.aclose()
    asyncio.run(run())
    curl.get.assert_not_awaited()


def test_background_requests_blocked_after_login(tmp_path, monkeypatch):
    owner, _ = make_owner(tmp_path, monkeypatch, [])
    route = SimpleNamespace(request=SimpleNamespace(url=URL, resource_type="fetch"),
                            continue_=AsyncMock(), abort=AsyncMock())
    asyncio.run(owner.route(route))
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


def test_login_dependencies_allowed_but_collection_waits_for_auth(tmp_path, monkeypatch):
    owner, _ = make_owner(tmp_path, monkeypatch, [])
    owner.login_mode = True
    async def run():
        for path, allowed in [
            ("/api/security/v1/webprofile", True),
            ("/api/sns/web/v1/login/qrcode/status", True),
            ("/api/sns/web/v1/homefeed", False),
            ("/api/sns/web/v2/comment/page", False),
        ]:
            route = SimpleNamespace(request=SimpleNamespace(
                url="https://edith.xiaohongshu.com" + path, resource_type="fetch", method="GET"),
                continue_=AsyncMock(), abort=AsyncMock())
            await owner.route(route)
            assert route.continue_.await_count == int(allowed)
            assert route.abort.await_count == int(not allowed)
    asyncio.run(run())


def test_login_requires_authenticated_user_not_guest_or_qr_creation(tmp_path, monkeypatch):
    owner, _ = make_owner(tmp_path, monkeypatch, [])
    owner.login_mode = True
    owner.set_status("awaiting_scan")
    async def run():
        for path, data, expected in [
            ("/api/sns/web/v1/login/qrcode/create", {}, "awaiting_scan"),
            ("/api/sns/web/v2/user/me", {"guest": True}, "awaiting_scan"),
            ("/api/sns/web/v2/user/me", {"guest": False}, "ready"),
        ]:
            reply = SimpleNamespace(url="https://edith.xiaohongshu.com" + path, status=200,
                json=AsyncMock(return_value={"code": 0, "success": True, "data": data}))
            await owner.observe_login(reply)
            assert owner.status == expected
        assert not owner.login_mode
    asyncio.run(run())
    owner.context.storage_state.assert_awaited_once()
