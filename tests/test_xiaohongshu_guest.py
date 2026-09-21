import asyncio
from contextlib import asynccontextmanager
import json
import stat

import pytest

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.xiaohongshu_guest import XiaohongshuGuestSession, open_guest_session
from app.manually_execute_script import bootstrap_xiaohongshu_guest as bootstrap


class Response:
    def __init__(self, data, status=200):
        self.status_code = status
        self.payload = {"success": True, "code": 0, "data": data}

    def json(self):
        return self.payload


class GuestClient:
    def __init__(self, activated=None, identity=None):
        self.activated = activated or Response({"user_id": "visitor", "session": "private-session"})
        self.identity = identity or Response({"user_id": "visitor", "guest": True})
        self.active = 0
        self.calls = []
        self.failures = []

    @asynccontextmanager
    async def isolated_session_scope(self):
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1

    lease_scope = isolated_session_scope

    async def post(self, url, **kwargs):
        assert self.active == 2
        self.calls.append((url, kwargs))
        return self.activated

    async def get(self, url, **kwargs):
        assert self.active == 2
        self.calls.append((url, kwargs))
        return self.identity

    def invalidate_active_lease(self, reason):
        self.failures.append(reason)


def test_guest_is_verified_before_yield_and_scopes_cover_consumer():
    async def run():
        client = GuestClient()
        async with open_guest_session(client) as guest:
            assert client.active == 2
            assert len(client.calls) == 2
            assert guest.user_id == "visitor"
            assert "web_session=private-session" in guest.cookie
            assert "private-session" not in repr(guest)
            activate_cookie = client.calls[0][1]["headers"]["Cookie"]
            verify_cookie = client.calls[1][1]["headers"]["Cookie"]
            assert "web_session=" not in activate_cookie
            assert verify_cookie == activate_cookie + "; web_session=private-session"
        assert client.active == 0
        assert not client.failures
    asyncio.run(run())


@pytest.mark.parametrize("identity", [
    {"user_id": "visitor", "guest": False},
    {"user_id": "other", "guest": True},
    {"user_id": "visitor"},
])
def test_account_or_mismatched_identity_is_rejected(identity):
    async def run():
        client = GuestClient(identity=Response(identity))
        with pytest.raises(PlatformCrawlerError, match="同一游客身份"):
            async with open_guest_session(client):
                pytest.fail("unverified identity was exposed")
        assert client.active == 0
        assert len(client.failures) == 1
    asyncio.run(run())


@pytest.mark.parametrize("activated", [
    Response({"user_id": "visitor"}),
    Response({"user_id": "visitor", "session": "bad\r\nCookie: other"}),
    Response({"user_id": "visitor", "session": "bad; second=value"}),
    Response({}, status=461),
    Response([]),
])
def test_failed_activation_never_attempts_identity_or_leaks_session(activated):
    async def run():
        client = GuestClient(activated=activated)
        with pytest.raises(PlatformCrawlerError) as error:
            async with open_guest_session(client):
                pytest.fail("invalid activation was accepted")
        assert "Cookie" not in str(error.value)
        assert client.active == 0
        assert len(client.calls) == len(client.failures) == 1
    asyncio.run(run())


def test_cancelled_consumer_releases_scopes():
    async def run():
        client = GuestClient()
        with pytest.raises(asyncio.CancelledError):
            async with open_guest_session(client):
                raise asyncio.CancelledError()
        assert client.active == 0
    asyncio.run(run())


@pytest.mark.parametrize("fail", [False, True])
def test_cli_private_state_and_cleanup_without_secret_output(tmp_path, monkeypatch, capsys, fail):
    closed = []

    class Provider:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            closed.append("provider")

    class Client:
        def __init__(self, **kwargs):
            pass

        async def aclose(self):
            closed.append("client")

    @asynccontextmanager
    async def guest_session(client):
        if fail:
            raise PlatformCrawlerError("secret response")
        yield XiaohongshuGuestSession("visitor", "a1=private-device; web_session=private-cookie")

    monkeypatch.setattr(bootstrap, "AsyncDailiProxyProvider", Provider)
    monkeypatch.setattr(bootstrap, "CurlAsyncHttpClient", Client)
    monkeypatch.setattr(bootstrap, "open_guest_session", guest_session)
    output = tmp_path / "state.json"
    assert asyncio.run(bootstrap.run(output)) == int(fail)
    assert closed == ["client", "provider"]
    printed = capsys.readouterr().out
    assert "private-" not in printed and "secret response" not in printed
    if fail:
        assert not output.exists()
        assert len(printed.splitlines()) == 3
    else:
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert json.loads(output.read_text())["cookies"][1]["value"] == "private-cookie"
        assert json.loads(printed)["content_access_verified"] is False
