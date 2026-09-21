"""Account-free XHS web activation; guest identity is not content permission."""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from xhshow import Xhshow

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.xiaohongshu import authenticated_headers, signed_headers


ORIGIN = "https://www.xiaohongshu.com/"
API = "https://edith.xiaohongshu.com"


@dataclass(frozen=True)
class XiaohongshuGuestSession:
    user_id: str
    cookie: str = field(repr=False)


def _data(response, stage):
    if response.status_code != 200:
        raise PlatformCrawlerError(f"小红书游客{stage}失败 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise PlatformCrawlerError(f"小红书游客{stage}未返回 JSON") from None
    if (not isinstance(payload, dict) or payload.get("success") is not True
            or payload.get("code") != 0 or not isinstance(payload.get("data"), dict)):
        raise PlatformCrawlerError(f"小红书游客{stage}没有返回有效数据")
    return payload["data"]


@asynccontextmanager
async def open_guest_session(client):
    """Keep new device cookies and the same proxy for activation and subsequent use.

    Callers must separately verify each requested resource; activation does not
    grant account privileges or guarantee note/search/comment access.
    """
    async with client.isolated_session_scope(), client.lease_scope():
        a1 = Xhshow.generate_a1()
        cookie = f"a1={a1}; webId={Xhshow.generate_web_id(a1)}; xsecappid=xhs-pc-web"
        path = "/api/sns/web/v1/login/activate"
        headers = authenticated_headers(cookie, ORIGIN)
        headers.update(signed_headers(path, cookie, payload={}, sign_format="xyw"))
        headers.update({"Content-Type": "application/json", "Origin": ORIGIN.rstrip("/")})
        try:
            activated = _data(await client.post(API + path, headers=headers, data="{}"), "激活")
            user_id, session = activated.get("user_id"), activated.get("session")
            if (not isinstance(user_id, str) or not user_id
                    or not isinstance(session, str) or not session
                    or any(c in session for c in "\r\n;")):
                raise PlatformCrawlerError("小红书游客激活缺少有效身份或会话")
            cookie += f"; web_session={session}"
            path = "/api/sns/web/v2/user/me"
            headers = authenticated_headers(cookie, ORIGIN)
            headers.update(signed_headers(path, cookie, params={}, sign_format="xyw"))
            headers["Origin"] = ORIGIN.rstrip("/")
            identity = _data(await client.get(API + path, headers=headers), "身份验证")
            if identity.get("guest") is not True or identity.get("user_id") != user_id:
                raise PlatformCrawlerError("小红书未确认同一游客身份")
        except Exception:
            client.invalidate_active_lease("XHS guest activation or identity verification failed")
            raise
        yield XiaohongshuGuestSession(user_id=user_id, cookie=cookie)
