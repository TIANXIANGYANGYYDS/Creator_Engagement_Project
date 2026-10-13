"""Single local browser owner for XHS reads, cookies and account pacing.

The Unix socket is private to the service user. No credentials cross the RPC
boundary; the browser signs with its current cookies and sends native fetches.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import time
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import httpx

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platform_session import PlatformSessionStore


READ_API = {
    ("GET", "/api/sns/web/v2/user/me"),
    ("GET", "/api/sns/web/v1/user_posted"),
    ("GET", "/api/sns/web/v2/comment/page"),
    ("GET", "/api/sns/web/v2/comment/sub/page"),
    ("POST", "/api/sns/web/v1/search/usersearch"),
}
STOP_STATES = {"reauthorization_required", "risk_limited", "transport_stopped"}


def validate_read(method: str, url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}:
        raise ValueError("小红书浏览器只允许 HTTPS 只读请求")
    if parts.hostname == "edith.xiaohongshu.com" and (method, parts.path) in READ_API:
        return
    if method == "GET" and parts.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        if parts.path.startswith(("/explore/", "/discovery/item/", "/user/profile/")):
            return
    raise ValueError("小红书浏览器不支持此请求路径或方法")


async def browser_request(socket: str, method: str, url: str, **kwargs):
    validate_read(method, url)
    data = kwargs.get("data")
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    headers = kwargs.get("headers") or {}
    # Only the format hint is used; credentials and stale signatures stay local
    # to the caller. Browser cookies are the sole authority.
    sign_format = "xyw" if str(headers.get("X-S", "")).lower().startswith("xyw") else "xys"
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket),
            base_url="http://xhs-owner", timeout=240, trust_env=False,
        ) as client:
            response = await client.post("/request", json={
                "method": method, "url": url, "params": kwargs.get("params"),
                "data": data, "json_body": kwargs.get("json"), "sign_format": sign_format,
            })
    except httpx.HTTPError as exc:
        raise PlatformBlockedError("小红书常驻浏览器不可用，已停止请求；禁止回退代理或独立会话") from exc
    if response.status_code != 200:
        raise PlatformBlockedError(response.json().get("detail", "小红书常驻浏览器已停止请求"))
    body = response.json()
    return httpx.Response(body["status"], text=body["text"], headers=body.get("headers"),
                          request=httpx.Request(method, body["url"]))


class XiaohongshuSessionOwner:
    def __init__(self, context, page, store: PlatformSessionStore, root: Path, *, interval: float = 15):
        self.context, self.page, self.store, self.root = context, page, store, root
        self.interval = max(15, interval)
        self.lock = asyncio.Lock()
        self.last_end = 0.0
        self.count = 0
        self.errors = 0
        self.pending_url = ""
        self.status = "starting"
        self.login_mode = False
        self.last_success = None
        self.reason = ""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.qr_path = root / "login-qr.png"
        self.login_events = []

    def snapshot(self):
        return {"status": self.status, "reason": self.reason,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "last_success": self.last_success, "upstream_requests": self.count,
                "direct": True, "minimum_interval_seconds": self.interval}

    def set_status(self, status: str, reason: str = ""):
        self.status, self.reason = status, reason
        path = self.root / "status.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def check_ready(self):
        if self.status != "ready":
            raise PlatformBlockedError("小红书会话暂停：" + self.status + " " + self.reason)

    async def route(self, route):
        request = route.request
        if request.url == self.pending_url:
            await route.continue_()
        elif request.resource_type in {"xhr", "fetch", "websocket", "eventsource"}:
            # The login UI must poll its QR endpoint. Once authenticated, even
            # page-originated background API traffic must stop using the account.
            path = urlsplit(request.url).path
            if self.login_mode and not (
                (request.method, path) in READ_API and not path.endswith("/user/me")
                or "/homefeed" in path
            ):
                await route.continue_()
            else:
                await route.abort()
        else:
            await route.continue_()

    async def observe_login(self, response):
        path = urlsplit(response.url).path
        if not self.login_mode or not any(part in path for part in ("/login/", "/user/me")):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        self.login_events.append({"path": path, "http": response.status,
                                  "code": payload.get("code"), "success": payload.get("success"),
                                  "guest": data.get("guest") if isinstance(data, dict) else None})
        self.login_events = self.login_events[-10:]
        if path.endswith("/user/me") and payload.get("success") is True and isinstance(data, dict) and data.get("guest") is False:
            await self.store.save_context("xiaohongshu", self.context)
            self.last_success = datetime.now(timezone.utc).isoformat()
            self.login_mode = False
            self.last_end = time.monotonic()
            self.set_status("ready")
            self.qr_path.unlink(missing_ok=True)

    async def observe_network(self, response):
        # CORS can hide a rejected response from fetch(), but Playwright still
        # sees its HTTP status. Trip the stop before another queued read starts.
        if self.pending_url and response.url == self.pending_url:
            if response.status in {401, 403, 412, 418, 429, 432, 461, 471}:
                self.set_status("risk_limited", f"HTTP {response.status}")

    async def login(self):
        self.login_mode = True
        self.set_status("awaiting_scan")
        try:
            await self.page.goto("https://www.xiaohongshu.com/", wait_until="domcontentloaded", timeout=60_000)
            last_source = None
            for _ in range(600):
                if not self.login_mode:
                    return
                qr = self.page.locator("img.qrcode-img")
                source = await qr.first.get_attribute("src") if await qr.count() else None
                if source and source != last_source and source.startswith("data:image/"):
                    self.qr_path.write_bytes(base64.b64decode(source.partition(",")[2]))
                    self.qr_path.chmod(0o600)
                    last_source = source
                    print(f"登录二维码: {self.qr_path}", flush=True)
                await asyncio.sleep(1)
            self.set_status("reauthorization_required", "扫码等待超时；需要再次运行 --login")
        except Exception as exc:
            self.set_status("transport_stopped", "登录页面异常：" + type(exc).__name__)
        finally:
            self.login_mode = False

    def inspect(self, result):
        status = result["status"]
        try:
            payload = json.loads(result["text"])
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        code = payload.get("code")
        path = urlsplit(result["url"]).path
        data = payload.get("data")
        if code in {-100, -101} or (path.endswith("/user/me") and isinstance(data, dict) and data.get("guest") is True):
            self.set_status("reauthorization_required", f"登录失效，业务码 {code}")
        elif status in {401, 403, 412, 418, 429, 432, 461, 471} or code in {-104, -103, -13002, 461, 471}:
            self.set_status("risk_limited", f"HTTP {status}，业务码 {code}")
        elif any(part in path.lower() for part in ("/login", "captcha", "/security")):
            self.set_status("risk_limited", "页面要求验证")
        elif payload.get("success") is True:
            self.last_success = datetime.now(timezone.utc).isoformat()
        return code

    async def request(self, method: str, url: str, params=None, data=None, json_body=None, sign_format="xys"):
        validate_read(method, url)
        self.check_ready()
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=30)
        except TimeoutError as exc:
            raise PlatformBlockedError("小红书低速队列繁忙，请稍后再试") from exc
        try:
            self.check_ready()
            delay = self.interval + random.uniform(0, 10)
            if self.count and self.count % 10 == 0:
                delay += 90
            wait = self.last_end + delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.check_ready()
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query.update(params or {})
            encoded = "&".join(f"{quote(str(k), safe='')}={quote(str(v), safe=',')}" for k, v in query.items())
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, ""))
            headers = {}
            body = json_body if json_body is not None else data
            if isinstance(body, str):
                body = json.loads(body)
            if parts.hostname == "edith.xiaohongshu.com":
                from app.crawlers.platforms.xiaohongshu import signed_headers

                cookies = await self.context.cookies([url])
                cookie = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
                headers = signed_headers(parts.path, cookie, params=query if method == "GET" else None,
                                         payload=body if method == "POST" else None, sign_format=sign_format)
            if body is not None:
                headers["Content-Type"] = "application/json"
            self.pending_url = url
            started = time.monotonic()
            record = {"at": datetime.now(timezone.utc).isoformat(), "method": method,
                      "endpoint": parts.hostname + parts.path,
                      "idle_seconds": round(started - self.last_end, 3) if self.last_end else None}
            self.count += 1
            try:
                result = await self.page.evaluate("""async ({url, method, headers, body}) => {
                    const r = await fetch(url, {method, headers, credentials: 'include',
                        redirect: 'error', signal: AbortSignal.timeout(30000),
                        ...(body === null ? {} : {body})});
                    return {status: r.status, text: await r.text(), url: r.url,
                            headers: {'content-type': r.headers.get('content-type') || ''}};
                }""", {"url": url, "method": method, "headers": headers,
                       "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else None})
                self.errors = 0
                record["status"] = result["status"]
                record["code"] = self.inspect(result)
                await self.store.save_context("xiaohongshu", self.context)
                self.check_ready()
                return result
            except PlatformBlockedError:
                raise
            except Exception as exc:
                self.check_ready()
                self.errors += 1
                record["exception_type"] = type(exc).__name__
                if self.errors >= 3:
                    self.set_status("transport_stopped", "连续 3 次网络或浏览器异常；未判定为登录失效")
                raise PlatformCrawlerError("小红书浏览器请求异常：" + type(exc).__name__) from exc
            finally:
                self.last_end = time.monotonic()
                self.pending_url = ""
                record["state"] = self.status
                with (self.root / "requests.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.set_status(self.status, self.reason)
        finally:
            self.lock.release()
